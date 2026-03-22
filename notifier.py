"""
Email notification module.

Sends formatted HTML email alerts when new listings matching criteria are found.
"""

import logging
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from evaluator import EvaluationResult
from scraper import Listing

logger = logging.getLogger(__name__)


def _format_score_bar(score: int, max_score: int = 10) -> str:
    """Create a visual score bar in HTML."""
    pct = (score / max_score) * 100
    if score >= 8:
        color = "#22c55e"
    elif score >= 6:
        color = "#eab308"
    elif score >= 4:
        color = "#f97316"
    else:
        color = "#ef4444"

    return (
        f'<div style="background:#e5e7eb;border-radius:4px;height:12px;width:120px;display:inline-block;vertical-align:middle;">'
        f'<div style="background:{color};border-radius:4px;height:12px;width:{pct}%;"></div>'
        f'</div> <strong>{score}</strong>/10'
    )


def _build_listing_html(listing: Listing, evaluation: EvaluationResult | None) -> str:
    """Build HTML for a single listing card."""
    # First photo as thumbnail
    photo_html = ""
    if listing.photo_urls:
        photo_html = (
            f'<img src="{listing.photo_urls[0]}" '
            f'style="width:100%;max-width:400px;border-radius:8px;margin-bottom:12px;" '
            f'alt="Listing photo">'
        )

    # Evaluation scores
    eval_html = ""
    if evaluation:
        eval_html = '<div style="margin-top:12px;padding:12px;background:#f8fafc;border-radius:8px;">'
        eval_html += '<strong style="color:#1e40af;">AI Evaluation</strong><br>'
        for name, score in evaluation.scores.items():
            reasoning = evaluation.reasoning.get(name, "")
            eval_html += (
                f'<div style="margin:6px 0;">'
                f'<span style="text-transform:capitalize;min-width:100px;display:inline-block;">{name}:</span> '
                f'{_format_score_bar(score)}'
                f'<br><span style="color:#6b7280;font-size:0.85em;margin-left:8px;">{reasoning}</span>'
                f'</div>'
            )
        eval_html += f'<div style="margin-top:8px;color:#374151;"><em>{evaluation.summary}</em></div>'
        eval_html += '</div>'

    price_str = f"${listing.price:,}" if listing.price else "Price N/A"
    beds_baths = f"{listing.beds or '?'}bd / {listing.baths or '?'}ba"
    sqft_str = f"{listing.sqft:,} sqft" if listing.sqft else "Sqft N/A"
    year_str = f"Built {listing.year_built}" if listing.year_built else ""

    url = listing.url
    if not url.startswith("http"):
        url = f"https://www.zillow.com{url}"

    return f"""
    <div style="border:1px solid #e5e7eb;border-radius:12px;padding:20px;margin-bottom:20px;max-width:600px;font-family:system-ui,-apple-system,sans-serif;">
        {photo_html}
        <h2 style="margin:0 0 8px 0;color:#1e293b;">
            <a href="{url}" style="color:#2563eb;text-decoration:none;">{listing.address}</a>
        </h2>
        <div style="font-size:1.4em;font-weight:bold;color:#059669;margin-bottom:8px;">{price_str}</div>
        <div style="color:#475569;margin-bottom:4px;">
            {beds_baths} &bull; {sqft_str} {('&bull; ' + year_str) if year_str else ''}
        </div>
        <div style="color:#64748b;font-size:0.9em;margin-bottom:4px;">
            {listing.home_type}
            {f' &bull; {listing.days_on_zillow} days on Zillow' if listing.days_on_zillow is not None else ''}
            {f' &bull; Zestimate: ${listing.zestimate:,}' if listing.zestimate else ''}
        </div>
        {f'<p style="color:#475569;font-size:0.9em;margin-top:8px;">{listing.description[:300]}{"..." if len(listing.description) > 300 else ""}</p>' if listing.description else ''}
        {eval_html}
        <div style="margin-top:12px;">
            <a href="{url}" style="display:inline-block;padding:8px 20px;background:#2563eb;color:white;text-decoration:none;border-radius:6px;font-weight:bold;">View on Zillow</a>
        </div>
    </div>
    """


def _build_email_html(
    listings: list[tuple[Listing, EvaluationResult | None]],
    config: dict,
) -> str:
    """Build full HTML email body."""
    location = config["search"].get("location", "Unknown")
    count = len(listings)

    listings_html = ""
    for listing, evaluation in listings:
        listings_html += _build_listing_html(listing, evaluation)

    return f"""
    <!DOCTYPE html>
    <html>
    <body style="background:#f1f5f9;padding:20px;font-family:system-ui,-apple-system,sans-serif;">
        <div style="max-width:640px;margin:0 auto;">
            <div style="text-align:center;margin-bottom:24px;">
                <h1 style="color:#1e293b;margin-bottom:4px;">Zillow Monitor Alert</h1>
                <p style="color:#64748b;margin:0;">
                    {count} new listing{'s' if count != 1 else ''} found in {location}
                </p>
            </div>
            {listings_html}
            <div style="text-align:center;color:#94a3b8;font-size:0.8em;margin-top:32px;">
                <p>Sent by Zillow House Monitor</p>
            </div>
        </div>
    </body>
    </html>
    """


def send_email_notification(
    listings: list[tuple[Listing, EvaluationResult | None]],
    config: dict,
) -> bool:
    """
    Send an email notification with matched listings.

    Args:
        listings: List of (Listing, EvaluationResult) tuples.
        config: Full app config dict.

    Returns:
        True if email was sent successfully.
    """
    email_config = config.get("notifications", {}).get("email", {})

    if not email_config.get("enabled", False):
        logger.info("Email notifications disabled")
        return False

    if not listings and not config.get("notifications", {}).get("send_empty_reports", False):
        logger.info("No listings to report and empty reports disabled")
        return False

    # Get credentials from env vars or config
    username = os.environ.get("ZILLOW_EMAIL_USER", email_config.get("username", ""))
    password = os.environ.get("ZILLOW_EMAIL_PASSWORD", email_config.get("password", ""))
    from_addr = os.environ.get("ZILLOW_EMAIL_FROM", email_config.get("from_address", username))
    to_addrs = email_config.get("to_addresses", [])

    if not username or not password:
        logger.error(
            "Email credentials not configured. Set ZILLOW_EMAIL_USER and "
            "ZILLOW_EMAIL_PASSWORD environment variables."
        )
        return False

    if not to_addrs or not any(to_addrs):
        logger.error("No recipient email addresses configured")
        return False

    # Filter empty addresses
    to_addrs = [a for a in to_addrs if a]

    smtp_host = email_config.get("smtp_host", "smtp.gmail.com")
    smtp_port = email_config.get("smtp_port", 587)
    use_tls = email_config.get("use_tls", True)

    # Build email
    location = config["search"].get("location", "")
    count = len(listings)
    subject = f"Zillow Alert: {count} new listing{'s' if count != 1 else ''} in {location}"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = ", ".join(to_addrs)

    # Plain text fallback
    plain_text = f"{count} new listings found in {location}:\n\n"
    for listing, evaluation in listings:
        url = listing.url if listing.url.startswith("http") else f"https://www.zillow.com{listing.url}"
        plain_text += f"- {listing.address}: ${listing.price:,} | {listing.beds}bd/{listing.baths}ba\n"
        plain_text += f"  {url}\n"
        if evaluation:
            for name, score in evaluation.scores.items():
                plain_text += f"  {name}: {score}/10\n"
        plain_text += "\n"

    msg.attach(MIMEText(plain_text, "plain"))

    # HTML version
    html = _build_email_html(listings, config)
    msg.attach(MIMEText(html, "html"))

    # Send
    try:
        if use_tls:
            server = smtplib.SMTP(smtp_host, smtp_port)
            server.ehlo()
            server.starttls()
        else:
            server = smtplib.SMTP_SSL(smtp_host, smtp_port)

        server.login(username, password)
        server.sendmail(from_addr, to_addrs, msg.as_string())
        server.quit()

        logger.info("Email notification sent to %s", ", ".join(to_addrs))
        return True

    except smtplib.SMTPException:
        logger.error("Failed to send email notification", exc_info=True)
        return False
