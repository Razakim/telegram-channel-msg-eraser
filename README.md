# Razakim Channel Eraser

Razakim Channel Eraser is a hosted Telegram channel purge bot by Savadogo
RAZAKIM / Razakim tech. It is built from the MIT-licensed TgEraser project and
adds a Koyeb-ready control bot, inline buttons, slow resumable deletion, daily
limits and safer FloodWait handling.

The legacy CLI is still available for compatibility, but the main workflow is
now the hosted channel purge service.

## Installation

```
pip install -e .
tgeraser
```

To use the project, you'll need to provide `api_id` and `api_hash`, which you can obtain from [here](https://my.telegram.org/auth?to=apps).

There are two methods to define `api_id` and `api_hash`:
1. Set them as environment variables (`TG_API_ID` and `TG_API_HASH`).
2. Allow the tool to prompt you for input during first execution, with an option to save the credentials in a `credentials.json` file located in the same directory as the sessions (by default, `~/.tgeraser/`).
Credentials file can be created/edited manually in the following format:
```json
{
    "api_id": 111111,
    "api_hash": "abcdef1234567890abcdef1234567890"
}
```

## Usage

```
Razakim Channel Eraser legacy CLI deletes your messages from a chat, channel, or conversation on Telegram.

Usage:
    tgeraser [(session <session_name>) --entity-type TYPE -l NUM -d PATH -p PEER_ID -o STRING -m TYPES --delete-conversation]
    tgeraser session <session_name> -w [--entity-type TYPE -o STRING -m TYPES --delete-conversation]
    tgeraser -h | --help
    tgeraser --version

Options:
    -d --directory PATH         Specify a directory where your sessions are stored. [default: ~/.tgeraser/]
    -w --wipe-everything        Delete all messages from all entities of a certain type that you have in your dialog list.
    --delete-conversation       If set, delete the whole conversation (only valid for user-type peers).
    --entity-type TYPE          Available types: any, chat, channel, user. [default: chat]
    -p --peers PEER_ID          Specify certain peers by comma (chat/channel/user).
    -l --limit NUM              Show a specified number of recent chats.
    -o --older-than STRING      Delete messages older than X seconds/minutes/hours/days/weeks.
                                Example: --older-than "3*days" OR --older-than "5*seconds"
    -m --media-type TYPES       Delete only specific media types (server-side filtering).
                                Comma-separated list of: photo, video, audio, voice, video_note, gif, document.
                                Use "media" to delete all media types. If not specified, deletes all messages.
                                Example: --media-type "photo,video" OR --media-type media
    --proxy HOST:PORT:SECRET    MTProto proxy (e.g. 1.2.3.4:443:deadbeef).
    -h --help                   Show this screen.
    --version                   Show version.
```

Executing the legacy CLI without options will guide you through the creation of your first user session. After that you can create sessions for multiple users using the `tgeraser session <new_session_name>` command.

## Hosted channel purge bot

The hosted control bot is designed for Koyeb. It uses:

- a Telegram user session through MTProto to read and delete old channel history;
- a Telegram bot token only for the inline-button control interface;
- a JSON state file so a purge can pause and resume without starting over;
- slow batches, daily limits and FloodWait handling to avoid aggressive API bursts.

The control bot exposes these inline actions:

- Status: show progress, last checkpoint and last error.
- Dry-run: estimate the channel message count without deleting.
- Launch: requires confirmation before deleting.
- Pause: stops cleanly after the current batch.
- Resume: continues from the last saved message ID.
- Settings and Help: never leave the user stuck without a back button.

### Generate the user session

Run this locally, not on Koyeb:

```
pip install -e .
tgeraser-session
```

Copy the printed value into Koyeb as `TG_SESSION_STRING`.

### Koyeb variables

Create a BotFather bot, add that bot to your channel if you want the UI close to the channel workflow, and set these environment variables:

```
TG_API_ID=123456
TG_API_HASH=your_api_hash
TG_BOT_TOKEN=123456:bot_token_from_botfather
TG_SESSION_STRING=telethon_string_session_generated_with_tgeraser-session
TG_CHANNEL=@your_channel_username
TG_CONTROL_USER_IDS=123456789
TG_STATE_DIR=/tmp/tgeraser
TG_BATCH_SIZE=50
TG_BATCH_DELAY_SECONDS=300
TG_DAILY_LIMIT=1000
PORT=8000
```

`TG_CONTROL_USER_IDS` is a comma-separated allow-list of Telegram numeric user IDs.
The MTProto user represented by `TG_SESSION_STRING` must be an admin of the target
channel and must have permission to delete messages.

For a five-day purge, set `TG_DAILY_LIMIT` to roughly:

```
total_messages / 5
```

For example, a channel with 50,000 messages should use about `10000`.
If you prefer a very conservative rhythm, keep smaller batches and a larger
`TG_BATCH_DELAY_SECONDS`.

### Run command

Koyeb can use the included `Procfile`:

```
web: tgeraser-service
```

You can also set the run command manually:

```
tgeraser-service
```

The service opens a tiny HTTP health endpoint on `$PORT` and keeps the Telegram
control bot running in the background.

## Contributing

If you have any issues or suggestions, please feel free to open an issue or submit a pull request.
