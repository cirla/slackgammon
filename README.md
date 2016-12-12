# Slackgammon

Slack adapter for GNU Backgammon

## Installation

* Install [GNU Backgammon](http://www.gnubg.com/index.php?itemid=22) (`gnubg`).
* Install [Python](https://www.python.org/) 3.5 or later and `pip install -U -r requirements.txt` (preferably in its own `virtualenv`).
* Create a Slack [slash command integration](https://api.slack.com/slash-commands) (e.g. `/slackgammon`) and note the token.
  The endpoint URL should be the location where you will be hosting the endpoint, e.g. https://example.com/slackgammon.
* Create a Slack [incoming webhooks integration](https://api.slack.com/incoming-webhooks) and note the endpoint URL.
* Run `slackgammon.py` with the required Slack info and desired configuration, e.g.
  ```shell
  python3.5 slackgammon.py --host "localhost" \
                           --port $SLACKGAMMON_PORT \
                           --slash-token $SLACKGAMMON_SLASH_TOKEN \
                           --webhook-url $SLACKGAMMON_WEBHOOK_URL \
                           --max-games 4 \
                           --gnubg-path $GNUBG_PATH
  ```
* Type `/slackgammon help` in Slack and enjoy!

