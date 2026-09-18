#!/usr/bin/env python3
"""
Delivery layer: turn alerts into an HTML report and send it to Slack and/or email.

Nothing is ever sent unless you pass --send. The default is a dry run that writes
a preview file and prints what would have gone out.

Configuration comes from environment variables (GitHub Actions secrets in the daily
job). No credentials are stored in this repo:

  SLACK_WEBHOOK_URL   Slack incoming-webhook URL
  SMTP_HOST           e.g. smtp.gmail.com
  SMTP_PORT           default 587
  SMTP_USER           SMTP username
  SMTP_PASSWORD       SMTP password or app password
  SMTP_STARTTLS       1 (default) or 0
  ALERT_FROM          From: address
  ALERT_TO            To: address(es), comma-separated
  DASHBOARD_URL       link to the published dashboard, shown in the report footer

Usage:
  python3 delivery.py                      # dry run -> data/report_preview.html
  python3 delivery.py --since 2026-09-01   # limit to recent inspections
  python3 delivery.py --send               # actually send (needs the env vars above)
  python3 delivery.py --send --channel slack
"""

import argparse
import html
import json
import os
import smtplib
import ssl
import sys
import urllib.error
import urllib.request
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path

import alerts as alerts_mod

PREVIEW_PATH = Path("data") / "report_preview.html"

# Email-safe palette. Inline styles only -- no external CSS, no flexbox, no grid,
# because Outlook and Gmail strip or ignore all three.
STYLE = {
    "closure":      ("#fdf2f2", "#f0c7ca", "#a4191f"),
    "high_priority": ("#fff8ec", "#efd9b4", "#8a4f00"),
    "hp_jump":      ("#f1f7fb", "#c4dceb", "#1c5678"),
}
INK, MUTED, RULE = "#1f2328", "#59636e", "#d8dee4"


# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────
class Config:
    def __init__(self, env=None):
        e = env if env is not None else os.environ
        self.slack_webhook = e.get("SLACK_WEBHOOK_URL", "").strip()
        self.smtp_host = e.get("SMTP_HOST", "").strip()
        self.smtp_port = int(e.get("SMTP_PORT", "587") or 587)
        self.smtp_user = e.get("SMTP_USER", "").strip()
        self.smtp_password = e.get("SMTP_PASSWORD", "")
        self.smtp_starttls = (e.get("SMTP_STARTTLS", "1").strip() != "0")
        self.mail_from = e.get("ALERT_FROM", "").strip()
        self.mail_to = [x.strip() for x in e.get("ALERT_TO", "").split(",") if x.strip()]
        self.dashboard_url = e.get("DASHBOARD_URL", "").strip()

    @property
    def slack_ready(self):
        return bool(self.slack_webhook)

    @property
    def email_ready(self):
        return bool(self.smtp_host and self.mail_from and self.mail_to)

    def describe(self):
        """Human-readable status. Deliberately never prints a secret value."""
        lines = []
        lines.append(f"  Slack : {'configured' if self.slack_ready else 'NOT configured (SLACK_WEBHOOK_URL unset)'}")
        if self.email_ready:
            lines.append(f"  Email : {self.smtp_host}:{self.smtp_port} "
                         f"{'STARTTLS' if self.smtp_starttls else 'plain'}, "
                         f"from {self.mail_from} to {', '.join(self.mail_to)}"
                         f"{' (no SMTP_USER -- unauthenticated)' if not self.smtp_user else ''}")
        else:
            missing = [n for n, v in (("SMTP_HOST", self.smtp_host),
                                      ("ALERT_FROM", self.mail_from),
                                      ("ALERT_TO", self.mail_to)) if not v]
            lines.append(f"  Email : NOT configured (missing {', '.join(missing)})")
        return "\n".join(lines)


# ─────────────────────────────────────────────
# RENDERING
# ─────────────────────────────────────────────
TIER_ORDER = ["closed", "shame", "redeemed", "fame", "neutral"]


def order_counts(counts):
    """Tier counts in severity order rather than whatever order the dict came in."""
    return [(k, counts[k]) for k in TIER_ORDER if k in counts] + \
           [(k, v) for k, v in counts.items() if k not in TIER_ORDER]


def subject_line(groups):
    n_closed = sum(1 for g in groups if "closure" in g["kinds"])
    n_serious = sum(1 for g in groups if "high_priority" in g["kinds"])
    n_jump = sum(1 for g in groups if "hp_jump" in g["kinds"] and "high_priority" not in g["kinds"])
    bits = []
    if n_closed:
        bits.append(f"{n_closed} closure{'s' if n_closed != 1 else ''}")
    if n_serious:
        bits.append(f"{n_serious} with 5+ high-priority")
    if n_jump:
        bits.append(f"{n_jump} sharply worse")
    what = ", ".join(bits) if bits else "no new alerts"
    return f"Indian River restaurant inspections — {what}"


def _badges_html(kinds):
    out = []
    for k in kinds:
        bg, border, fg = STYLE[k]
        label = alerts_mod.KIND_LABEL[k]
        out.append(
            f'<span style="display:inline-block;background:{bg};border:1px solid {border};'
            f'color:{fg};font-size:11px;font-weight:700;letter-spacing:.02em;'
            f'padding:3px 8px;border-radius:10px;margin:0 6px 4px 0;white-space:nowrap;">'
            f'{html.escape(label)}</span>')
    return "".join(out)


def render_html(groups, counts, dashboard_url="", generated_at=None):
    gen = generated_at or datetime.now().strftime("%A %d %B %Y, %H:%M")
    esc = html.escape

    if counts:
        chips = " · ".join(f'{esc(k.title())} {v}' for k, v in order_counts(counts))
        counts_row = (f'<tr><td style="padding:0 28px 18px;color:{MUTED};font-size:13px;">'
                      f'Current standings: {chips}</td></tr>')
    else:
        counts_row = ""

    if not groups:
        body = (f'<tr><td style="padding:8px 28px 28px;color:{MUTED};font-size:15px;'
                f'line-height:1.6;">No new closures or serious violations since the last '
                f'run. The dashboard is still up to date.</td></tr>')
    else:
        cards = []
        for g in groups:
            p = g["payload"]
            bg, border, fg = STYLE[g["kinds"][0]]
            rows = []
            if p.get("condition"):
                reopen = p.get("reopen_date")
                rows.append(("Condition", esc(p["condition"]) +
                             (f' — reopened {esc(reopen)}' if reopen
                              else ' — <strong>not yet reopened</strong>')))
            if p.get("high_priority") is not None:
                v = (f'<strong>{p["high_priority"]}</strong> high priority, '
                     f'{p.get("intermediate", 0)} intermediate, {p.get("basic", 0)} basic')
                if "previous_high_priority" in p:
                    when = f' on {esc(p["previous_date"])}' if p.get("previous_date") else ""
                    v += (f' <span style="color:{fg};">(was {p["previous_high_priority"]}'
                          f' at the last routine inspection{when}, up {p["jump"]})</span>')
                rows.append(("Violations", v))
            if p.get("disposition"):
                rows.append(("Result", esc(p["disposition"])))
            if p.get("inspection_type"):
                rows.append(("Inspection", esc(p["inspection_type"])))

            notes_html = ""
            if p.get("notes"):
                items = []
                for nt in p["notes"]:
                    flags = ""
                    if nt.get("flags"):
                        flags = (f' <span style="color:{fg};font-weight:700;">'
                                 f'({esc(", ".join(nt["flags"]))})</span>')
                    sev = esc(nt.get("severity") or "")
                    items.append(
                        f'<li style="margin:0 0 7px;color:{INK};font-size:13px;'
                        f'line-height:1.55;">'
                        f'<span style="color:{fg};font-weight:700;">{sev}</span>'
                        f' &middot; {esc(nt["text"])}{flags}</li>')
                notes_html = (
                    f'<div style="margin-top:14px;padding-top:12px;'
                    f'border-top:1px solid {border};">'
                    f'<div style="font-size:11px;font-weight:700;letter-spacing:.06em;'
                    f'text-transform:uppercase;color:{MUTED};margin-bottom:8px;">'
                    f'What the inspector found</div>'
                    f'<ul style="margin:0;padding-left:18px;">{"".join(items)}</ul></div>')

            detail = ""
            if p.get("detail_url"):
                detail = (f'<div style="margin-top:12px;"><a href="{esc(p["detail_url"])}" '
                          f'style="color:#0a5cab;font-size:13px;font-weight:600;'
                          f'text-decoration:none;">Read the full inspection report &rarr;</a></div>')

            detail_rows = "".join(
                f'<tr><td style="padding:2px 12px 2px 0;color:{MUTED};font-size:12px;'
                f'vertical-align:top;white-space:nowrap;">{k}</td>'
                f'<td style="padding:2px 0;color:{INK};font-size:13px;line-height:1.5;">{v}</td></tr>'
                for k, v in rows)

            where = " · ".join(x for x in (p.get("address"), p.get("city")) if x)
            cards.append(f"""
      <tr><td style="padding:0 28px 14px;">
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
               style="background:{bg};border:1px solid {border};border-radius:8px;">
          <tr><td style="padding:16px 18px;">
            <div style="font-size:17px;font-weight:700;color:{INK};line-height:1.3;">
              {esc(g["name"] or "(name not recorded)")}</div>
            <div style="color:{MUTED};font-size:12px;margin:3px 0 10px;">
              {esc(where)} · inspected {esc(g["date"] or "date unknown")}</div>
            <div>{_badges_html(g["kinds"])}</div>
            <table role="presentation" cellpadding="0" cellspacing="0"
                   style="margin-top:10px;">{detail_rows}</table>
            {notes_html}
            {detail}
          </td></tr>
        </table>
      </td></tr>""")
        body = "".join(cards)

    footer_link = ""
    if dashboard_url:
        footer_link = (f' &nbsp;·&nbsp; <a href="{esc(dashboard_url)}" '
                       f'style="color:#0a5cab;text-decoration:none;">Open the dashboard</a>')

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(subject_line(groups))}</title></head>
<body style="margin:0;padding:0;background:#f4f6f8;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0"
       style="background:#f4f6f8;padding:24px 12px;">
<tr><td align="center">
  <table role="presentation" width="640" cellpadding="0" cellspacing="0"
         style="width:100%;max-width:640px;background:#ffffff;border:1px solid {RULE};
                border-radius:10px;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',
                Roboto,Helvetica,Arial,sans-serif;">
    <tr><td style="padding:26px 28px 6px;">
      <div style="font-size:12px;font-weight:700;letter-spacing:.08em;color:{MUTED};
                  text-transform:uppercase;">Indian River County</div>
      <div style="font-size:23px;font-weight:800;color:{INK};margin-top:4px;line-height:1.25;">
        Restaurant inspection alerts</div>
      <div style="color:{MUTED};font-size:13px;margin-top:6px;">{esc(gen)}</div>
    </td></tr>
    <tr><td style="padding:16px 28px 18px;">
      <div style="height:1px;background:{RULE};"></div></td></tr>
    {counts_row}
    {body}
    <tr><td style="padding:6px 28px 24px;">
      <div style="height:1px;background:{RULE};margin-bottom:14px;"></div>
      <div style="color:{MUTED};font-size:11px;line-height:1.6;">
        Source: Florida DBPR, Division of Hotels &amp; Restaurants — public inspection
        extracts for county 41. Each report is a snapshot of conditions at the time of
        inspection.{footer_link}</div>
    </td></tr>
  </table>
</td></tr></table>
</body></html>"""


def render_text(groups, counts, dashboard_url=""):
    out = ["INDIAN RIVER COUNTY — RESTAURANT INSPECTION ALERTS",
           datetime.now().strftime("%A %d %B %Y, %H:%M"), ""]
    if counts:
        out.append("Current standings: "
                   + " · ".join(f"{k.title()} {v}" for k, v in order_counts(counts)))
        out.append("")
    if not groups:
        out.append("No new closures or serious violations since the last run.")
    for g in groups:
        p = g["payload"]
        out.append(f"* {g['name']} — {p.get('city','')} ({g['date']})")
        out.append(f"  {', '.join(alerts_mod.KIND_LABEL[k] for k in g['kinds'])}")
        if p.get("condition"):
            out.append(f"  Condition: {p['condition']}; reopened {p.get('reopen_date') or 'not yet'}")
        if p.get("high_priority") is not None:
            line = (f"  Violations: {p['high_priority']} high priority, "
                    f"{p.get('intermediate',0)} intermediate, {p.get('basic',0)} basic")
            if "previous_high_priority" in p:
                line += (f" (was {p['previous_high_priority']} at the last routine inspection"
                         f"{' on ' + p['previous_date'] if p.get('previous_date') else ''}, up {p['jump']})")
            out.append(line)
        for nt in p.get("notes", []):
            flags = f" ({', '.join(nt['flags'])})" if nt.get("flags") else ""
            out.append(f"    - {nt.get('severity')}: {nt['text']}{flags}")
        if p.get("detail_url"):
            out.append(f"  {p['detail_url']}")
        out.append("")
    out.append("Source: Florida DBPR, Division of Hotels & Restaurants (county 41).")
    if dashboard_url:
        out.append(dashboard_url)
    return "\n".join(out)


def render_slack(groups, counts, dashboard_url=""):
    """Slack incoming-webhook payload. Slack caps a message at 50 blocks."""
    blocks = [{"type": "header",
               "text": {"type": "plain_text", "text": "Indian River inspection alerts"}}]
    if counts:
        blocks.append({"type": "context", "elements": [{
            "type": "mrkdwn",
            "text": " · ".join(f"*{k.title()}* {v}" for k, v in order_counts(counts))}]})
    if not groups:
        blocks.append({"type": "section", "text": {
            "type": "mrkdwn",
            "text": "_No new closures or serious violations since the last run._"}})
    shown, remaining = groups[:12], max(0, len(groups) - 12)
    for g in shown:
        p = g["payload"]
        icon = {"closure": ":rotating_light:", "high_priority": ":warning:",
                "hp_jump": ":chart_with_upwards_trend:"}[g["kinds"][0]]
        lines = [f"{icon} *{g['name']}* — {p.get('city','')}  ·  _{g['date']}_",
                 "· " + ", ".join(alerts_mod.KIND_LABEL[k] for k in g["kinds"])]
        if p.get("condition"):
            lines.append(f"· Condition: {p['condition']} "
                         f"(reopened {p.get('reopen_date') or '_not yet_'})")
        if p.get("high_priority") is not None:
            t = f"· {p['high_priority']} high priority, {p.get('total_violations')} total"
            if "previous_high_priority" in p:
                t += (f" — up {p['jump']} from {p['previous_high_priority']} at the last routine"
                      f" inspection{' (' + p['previous_date'] + ')' if p.get('previous_date') else ''}")
            lines.append(t)
        for nt in p.get("notes", [])[:3]:
            flags = f" _({', '.join(nt['flags'])})_" if nt.get("flags") else ""
            text = nt["text"][:220] + ("…" if len(nt["text"]) > 220 else "")
            lines.append(f"    > *{nt.get('severity')}* — {text}{flags}")
        if p.get("detail_url"):
            lines.append(f"· <{p['detail_url']}|Full inspection report>")
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}})
    if remaining:
        blocks.append({"type": "context", "elements": [
            {"type": "mrkdwn", "text": f"_…and {remaining} more._"}]})
    if dashboard_url:
        blocks.append({"type": "context", "elements": [
            {"type": "mrkdwn", "text": f"<{dashboard_url}|Open the dashboard>"}]})
    return {"text": subject_line(groups), "blocks": blocks[:50]}


# ─────────────────────────────────────────────
# SENDING
# ─────────────────────────────────────────────
def send_slack(payload, cfg):
    req = urllib.request.Request(
        cfg.slack_webhook, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return True, f"Slack responded {r.status} {r.read(200).decode('utf-8', 'replace')}"
    except urllib.error.HTTPError as e:
        return False, f"Slack HTTP {e.code}: {e.read(300).decode('utf-8', 'replace')}"
    except Exception as e:
        return False, f"Slack {type(e).__name__}: {e}"


def send_email(subject, html_body, text_body, cfg):
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = cfg.mail_from
    msg["To"] = ", ".join(cfg.mail_to)
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")
    try:
        if cfg.smtp_port == 465:
            server = smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port,
                                      timeout=60, context=ssl.create_default_context())
        else:
            server = smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=60)
        with server:
            if cfg.smtp_port != 465 and cfg.smtp_starttls:
                server.starttls(context=ssl.create_default_context())
            if cfg.smtp_user:
                server.login(cfg.smtp_user, cfg.smtp_password)
            server.send_message(msg)
        return True, f"email sent to {', '.join(cfg.mail_to)}"
    except Exception as e:
        return False, f"email {type(e).__name__}: {e}"


# ─────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(alerts_mod.DB_PATH))
    ap.add_argument("--since", help="only consider inspections/closures on or after YYYY-MM-DD")
    ap.add_argument("--send", action="store_true",
                    help="actually deliver. Without this flag nothing leaves the machine.")
    ap.add_argument("--channel", choices=["both", "slack", "email"], default="both")
    ap.add_argument("--year", type=int, default=datetime.now().year,
                    help="which view's tier counts to show in the header")
    ap.add_argument("--out", default=str(PREVIEW_PATH), help="where to write the HTML preview")
    ap.add_argument("--empty-ok", action="store_true",
                    help="send even when there are no new alerts (a quiet all-clear)")
    args = ap.parse_args()

    conn = alerts_mod.connect(args.db)
    pending = alerts_mod.detect(conn, since=args.since)
    groups = alerts_mod.group_for_display(pending)
    counts = alerts_mod.tier_counts(conn, args.year)

    cfg = Config()
    subject = subject_line(groups)
    html_body = render_html(groups, counts, cfg.dashboard_url)
    text_body = render_text(groups, counts, cfg.dashboard_url)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html_body)

    print(f"pending: {len(pending)} triggers across {len(groups)} establishments")
    print(f"subject: {subject}")
    print(f"preview: {out}  ({out.stat().st_size/1024:,.0f} KB)")
    print("channels:")
    print(cfg.describe())

    if not groups and not args.empty_ok:
        print("\nnothing new -- not sending (use --empty-ok to send an all-clear anyway)")
        conn.close()
        return 0

    if not args.send:
        print("\nDRY RUN. Nothing was sent. Re-run with --send to deliver.")
        print("\n--- Slack payload preview ---")
        print(json.dumps(render_slack(groups, counts, cfg.dashboard_url), indent=1)[:1200])
        conn.close()
        return 0

    results, ok_any = [], False
    if args.channel in ("both", "slack"):
        if cfg.slack_ready:
            ok, detail = send_slack(render_slack(groups, counts, cfg.dashboard_url), cfg)
            results.append(("slack", ok, detail)); ok_any = ok_any or ok
        else:
            results.append(("slack", False, "skipped: SLACK_WEBHOOK_URL not set"))
    if args.channel in ("both", "email"):
        if cfg.email_ready:
            ok, detail = send_email(subject, html_body, text_body, cfg)
            results.append(("email", ok, detail)); ok_any = ok_any or ok
        else:
            results.append(("email", False, "skipped: SMTP_HOST/ALERT_FROM/ALERT_TO not set"))

    print()
    for name, ok, detail in results:
        print(f"  [{'ok' if ok else 'FAILED'}] {name}: {detail}")

    if ok_any:
        alerts_mod.record(conn, pending, sent=True)
        print(f"\nrecorded {len(pending)} alerts as sent -- they will not fire again")
    else:
        print("\nnothing delivered, so no alerts were marked as sent; they stay pending")
    conn.close()
    return 0 if ok_any else 1


if __name__ == "__main__":
    sys.exit(main())
