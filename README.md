# Zillow House Monitor

Monitors Zillow for houses matching your criteria and sends email notifications. Uses Claude's vision capabilities to evaluate listing photos for kitchen quality, yard space, curb appeal, and any other aesthetic criteria you define.

## Features

- **Configurable search filters**: price, beds/baths, sqft, home type, year built, and more
- **LLM-powered evaluation**: Claude analyzes listing photos to score kitchen, yard, and appearance
- **Email notifications**: Beautiful HTML emails with photos, details, and AI scores
- **Deduplication**: Only alerts on new listings you haven't seen before
- **Scheduled monitoring**: Run continuously on a configurable interval

## Quick Start

```bash
# Install dependencies
pip install -r requirements.txt

# Set environment variables
export ANTHROPIC_API_KEY="your-api-key"
export ZILLOW_EMAIL_USER="you@gmail.com"
export ZILLOW_EMAIL_PASSWORD="your-app-password"

# Edit config.yaml with your search criteria
# Then run:
python monitor.py --dry-run    # Test without sending email
python monitor.py              # Run once
python monitor.py --loop       # Run continuously
```

## Configuration

Edit `config.yaml` to set:

- **Search criteria**: location, price range, beds/baths, sqft, home type, year built, features
- **LLM evaluation**: enable/disable, criteria descriptions, minimum scores per criterion
- **Notifications**: SMTP settings, recipient addresses
- **Schedule**: check interval, deduplication settings

### Custom Evaluation Criteria

Add any criteria under `evaluation.criteria` in the config:

```yaml
evaluation:
  criteria:
    kitchen:
      description: "Modern kitchen with granite countertops and stainless appliances"
      min_score: 7
    yard:
      description: "Large backyard with mature trees"
      min_score: 5
    garage:
      description: "Clean, spacious garage for 2+ cars"
      min_score: 6
```

### Gmail Setup

For Gmail, use an [App Password](https://support.google.com/accounts/answer/185833):

1. Enable 2-factor authentication on your Google account
2. Go to Security > App Passwords
3. Generate a password for "Mail"
4. Use that as `ZILLOW_EMAIL_PASSWORD`

## CLI Options

```
python monitor.py [options]

  --config, -c FILE    Config file path (default: config.yaml)
  --loop               Run continuously on schedule
  --dry-run, -n        Search and evaluate without sending email
  --reset-seen         Clear the seen listings cache
```
