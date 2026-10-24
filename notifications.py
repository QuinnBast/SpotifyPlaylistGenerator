import os
import logging
import requests


class Notifier:
    """Sends notifications to every configured channel: a Discord webhook and/or ntfy.

    Reads the `notifications` section of config.json; environment variables take precedence so secrets (the webhook
    URL) don't need to be in the config file."""

    def __init__(self, notifications_config):
        ntfy_config = notifications_config.get('ntfy', {})
        discord_config = notifications_config.get('discord', {})

        self.ntfy_url = os.environ.get('NTFY_URL', ntfy_config.get('url'))
        self.ntfy_enabled = ntfy_config.get('enabled', False) and bool(self.ntfy_url)

        self.discord_webhook_url = os.environ.get('DISCORD_WEBHOOK_URL', discord_config.get('webhookUrl'))
        # Optional mention so urgent messages ping you, e.g. "<@123456789012345678>" (a Discord user ID).
        self.discord_mention = os.environ.get('DISCORD_MENTION', discord_config.get('mention', ''))
        self.discord_enabled = bool(self.discord_webhook_url) and (
            discord_config.get('enabled', True) or 'DISCORD_WEBHOOK_URL' in os.environ)

    def notify(self, title, message, url=None, urgent=False):
        """Sends a notification. `url` becomes a clickable link; `urgent` adds the Discord mention / ntfy priority."""
        if not self.discord_enabled and not self.ntfy_enabled:
            logging.warning(f"{title}: {message} (no notifications configured - set DISCORD_WEBHOOK_URL or NTFY_URL)")
            return

        if self.discord_enabled:
            self._send_discord(title, message, url, urgent)
        if self.ntfy_enabled:
            self._send_ntfy(title, message, url, urgent)

    def _send_discord(self, title, message, url, urgent):
        try:
            embed = {"title": title, "description": message}
            if url:
                embed["url"] = url
                embed["description"] = f"{message}\n\n[Log in to Spotify]({url})"
            response = requests.post(self.discord_webhook_url, json={
                "content": self.discord_mention if urgent and self.discord_mention else "",
                "embeds": [embed],
            }, timeout=30)
            response.raise_for_status()
            logging.info(f"Sent Discord notification: {title}")
        except Exception as notify_error:
            logging.error(f"Failed to send Discord notification: {notify_error}")

    def _send_ntfy(self, title, message, url, urgent):
        try:
            headers = {"Title": title, "Priority": "urgent" if urgent else "default"}
            if url:
                headers["Click"] = url
            requests.post(self.ntfy_url, data=message.encode('utf-8'), headers=headers, timeout=30)
            logging.info(f"Sent ntfy notification: {title}")
        except Exception as notify_error:
            logging.error(f"Failed to send ntfy notification: {notify_error}")
