# shaper-sync

Synchronize a local directory with [Shaper Hub](https://hub.shapertools.com).

Built for [Shaper Origin](https://www.shapertools.com/en-us/origin) owners who
want to push and pull SVG files to and from the Hub without going through the
web UI. Shaper doesn't provide a public API, so this tool relies on a
reverse-engineered API.

Supports uploading new and modified local files to Shaper Hub, downloading
files from Shaper Hub to a local directory, recursive synchronization, dry-run
mode, and continuous watch mode using watchdog.

## Installation

```bash
pip install .
```

## Usage

Credentials can be passed as arguments or via environment variables
`SHAPER_EMAIL` and `SHAPER_PASSWORD`.

```bash
# One-shot upload sync (local → Hub)
shaper-sync ./my-designs --email user@example.com --password secret

# Using environment variables
export SHAPER_EMAIL=user@example.com
export SHAPER_PASSWORD=secret
shaper-sync ./my-designs

# Download files from Hub that are not present locally (Hub → local)
shaper-sync ./my-designs --download

# Sync to/from a specific remote folder
shaper-sync ./my-designs --remote-path /Projects
shaper-sync ./my-designs --download --remote-path /Projects

# Watch mode (initial sync + continuous monitoring)
shaper-sync ./my-designs --watch

# Dry-run (no changes made)
shaper-sync ./my-designs --dry-run
shaper-sync ./my-designs --download --dry-run

# Skip subdirectories
shaper-sync ./my-designs --no-recursive
shaper-sync ./my-designs --download --no-recursive

# Verbose output
shaper-sync ./my-designs --verbose
```

### Download mode

`--download` fetches any files present on Shaper Hub that do not already exist
in the local directory. Files already present locally are skipped. Folders are
mirrored recursively by default (use `--no-recursive` to disable).

Files and folders whose names contain characters not permitted on Windows
(`\ / : * ? " < > |`), reserved device names (`CON`, `NUL`, `COM1`–`COM9`,
etc.), or names ending with a space or period are skipped with a warning.

## Docker

Run a one-shot sync:

```bash
docker run --rm \
  -e SHAPER_EMAIL=user@example.com \
  -e SHAPER_PASSWORD=secret \
  -v /path/to/designs:/data \
  ghcr.io/naps/shaper-sync /data
```

Run in watch mode:

```bash
docker run --rm \
  -e SHAPER_EMAIL=user@example.com \
  -e SHAPER_PASSWORD=secret \
  -v /path/to/designs:/data \
  ghcr.io/naps/shaper-sync /data --watch
```

## Disclaimer
This project is in no way, shape, or form associated with Shaper Tools. Shaper Tools does not 
provide a public API, so this tool relies on a reverse engineered API developed by the contributors.