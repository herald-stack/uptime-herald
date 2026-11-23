# Uptime Herald

**Downtime happens. Find out quickly.**

Uptime Herald is a self-hosted uptime monitoring tool for the services you run: your
homelab, your projects, your production boxes. Point it at what matters and you'll find
out the moment it confirms a monitor has gone down.

No user accounts. No monthly fees. Deploy with Docker. Configure with YAML files.

## What it does

- Checks websites (HTTP), hosts (ping), and ports (TCP) on the schedule you choose.
- Alerts you by email, Discord, Slack, Mattermost, Rocket.Chat, Telegram, ntfy,
  CallMeBot (WhatsApp, Signal, and Facebook Messenger), or any webhook that accepts JSON.
- Sends alerts for every monitor or only for the tags you pick.
- Pauses checks during planned maintenance windows.
- Shows every monitor, its history, and its response times on a dashboard you can theme,
  with a mobile layout and a kiosk page that rotates through your monitors.
- Stores its data in MariaDB or PostgreSQL, either in the included Docker Compose setup or
  in a database you already run.
- Runs on x86-64/AMD64 and ARM64, with a native container image for each. Docker pulls the
  right one for your machine.

## Design

- **No login, no management interface.** All configuration lives in a few YAML files that
  you can keep in git, so a bad edit is easy to roll back.
- **Few dependencies.** Python and vanilla JavaScript.

## Get started

The [Quick Start Guide](https://uptimeherald.org/docs/quick-start/) takes you from nothing
to a running instance with one monitor already working.

## Documentation

The documentation covers installing, configuring, and running Uptime Herald. It is
available at [uptimeherald.org/docs](https://uptimeherald.org/docs/).

## Help and feedback

- Report a bug or suggest a change: [issues.uptimeherald.org](https://issues.uptimeherald.org).
  See [CONTRIBUTING.md](CONTRIBUTING.md) for what to include.
- Report a security problem privately: see [SECURITY.md](SECURITY.md).

## License

Uptime Herald is licensed under the [GNU Affero General Public License v3.0 only](LICENSE).
