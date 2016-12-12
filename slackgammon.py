#!/usr/bin/env python3.5

import argparse
import asyncio
import functools
import json

import aiohttp
from aiohttp import web


class StreamReadlines:
    """
    Async iterator to support reading all available lines from an asyncio.StreamReader,
    stopping at either EOF or after a timeout has elapsed with no new lines.

    Can be replaced with a cleaner async generator (https://www.python.org/dev/peps/pep-0525/)
    in Python 3.6.
    """

    def __init__(self, stream_reader, timeout=0.1):
        self.stream_reader = stream_reader
        self.timeout = timeout

    def __aiter__(self): # python 3.5.2 and later
    # async def __aiter__(self): # python 3.5.0 and 3.5.1
        return self

    async def __anext__(self):
        try:
            line = await asyncio.wait_for(self.stream_reader.readline(), self.timeout)
            if line:
                return line
        except asyncio.TimeoutError:
            pass

        raise StopAsyncIteration


class SlackTemplate:
    NewGame = ('{challenger} started a new game against {challenged}:\n'
               '```\n'
               '{board}'
               '```')
    Command = ('{player} attempted to `{command}`:\n'
               '```\n'
               '{gnubg_output}'
               '```')
    Quit = '{quitter} quit game against {opponent}'
    Info = ('There are currently {active_games}/{max_games} games:\n'
            '{games}')


class GnubgWorker:
    async def start(self, executable_path):
        self.proc = await asyncio.create_subprocess_exec(
                executable_path, '--tty', '--quiet',
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
        )

        # consume gnubg copyright output
        async for line in self.readlines():
            pass

    def writeline(self, text):
        self.proc.stdin.write(str.encode('{}\n'.format(text)))

    def readlines(self):
        return StreamReadlines(self.proc.stdout)

    async def command(self, command):
        self.writeline(command)

        # can be replaced with cleaner async comprehension (https://www.python.org/dev/peps/pep-0530/)
        # in Python 3.6
        lines = []
        async for line in self.readlines():
            lines.append(line.decode())

        return lines

    async def quit(self):
        await self.command('quit')
        await self.command('y')
        try:
            await asyncio.wait_for(self.proc.wait(), 0.1)
        except asyncio.TimeoutError:
            self.proc.kill()


class GnubgManager:
    COMMANDS = {
        'help': {
            'args': [],
            'help': 'Print a list of all commands',
        },
        'info': {
            'args': [],
            'help': 'Print info about running games.',
        },
        'new': {
            'args': ['player'],
            'help': 'Start a new game against <player> (default: gnubg)',
        },
        'move': {
            'args': ['from1', 'to1', '...'],
            'help': 'Move checkers',
        },
        'double': {
            'args': [],
            'help': 'Offer a double',
        },
        'roll': {
            'args': [],
            'help': 'Roll the dice',
        },
        'accept': {
            'args': [],
            'help': 'Accept a cube or resignation',
        },
        'redouble': {
            'args': [],
            'help': 'Accept the cube one level higher than it was offered',
        },
        'reject': {
            'args': [],
            'help': 'Reject a cube or resignation',
        },
        'resign': {
            'args': [],
            'help': 'Offer to end the current game',
        },
        'quit': {
            'args': [],
            'help': 'Quit active game',
        },
    }

    HELP_TEXT = 'Commands:\n{}'.format(
        '\n'.join(
            '{} {}: {}'.format(c, ' '.join('<{}>'.format(a) for a in d['args']), d['help'])
            for c, d in COMMANDS.items()
        )
    )

    def __init__(self, executable_path, max_games, webhook):
        self.executable_path = executable_path
        self.max_games = max_games
        self.webhook = webhook
        # TODO: use user ids instead of names for workers dict keys
        self.workers = {} # { (player1, player2): GnubgWorker }

    def game_required(turn_required=True):
        """
        Decorate async member functions to require a game in progress
        They should take additional params (player, opponent, worker)
        after the standard params (params, slack_params)

        If no game is in progress for user, return HTTPForbidden error
        """
        def wrap(f):
            @functools.wraps(f)
            async def wrapped(self, params, slack_params):
                user_name = slack_params['user_name']

                game = [(ps, w) for ps, w in self.workers.items() if user_name in ps]

                if not game:
                    return web.HTTPForbidden(text='You do not have a game in progress.')

                players, worker = game[0]
                opponent = next(p for p in players if p != user_name)

                gnubg_output = ''.join(await worker.command('show turn'))
                turn = gnubg_output.split()[0]
                if turn_required and turn != user_name:
                    return web.HTTPForbidden(text='It\'s not your turn!')

                return await f(self, params, slack_params, user_name, opponent, worker)

            return wrapped
        return wrap

    def run_command(f):
        """
        Decorate member functions to run the command in gnubg and post output to Slack.
        Requires @game_required decorator.
        """

        @functools.wraps(f)
        async def wrapped(self, params, slack_params, player, opponent, worker):
            command = ' '.join([f.__name__, *params])
            gnubg_output = await worker.command(command)

            slack_out = SlackTemplate.Command.format(
                player=player,
                command=command,
                gnubg_output=''.join(gnubg_output),
            )
            await self.webhook.post(slack_out, channel=slack_params['channel_id'])

            gnubg_output = ''.join(await worker.command('show turn'))
            if gnubg_output.startswith('No game'):
                await worker.quit()

            return web.Response()

        return wrapped

    async def help(self, params, slack_params): # pylint: disable=unused-argument
        return web.Response(text=self.HELP_TEXT)

    async def info(self, params, slack_params): # pylint: disable=unused-argument
        active = len(self.workers)
        games = '\n'.join('{} vs. {}'.format(p1, p2) for p1, p2 in self.workers.keys())
        return web.Response(text=SlackTemplate.Info.format(active_games=active, max_games=self.max_games, games=games))

    async def new(self, params, slack_params):
        if len(self.workers) == self.max_games:
            return web.HTTPServiceUnavailable(text='Max game limit reached. Try again after a game has finished.')

        user_name = slack_params['user_name']
        for players in self.workers.keys():
            if user_name in players:
                return web.HTTPForbidden(text='You already have a game in progress.')

        challenged = 'gnubg'
        if params and params[0] != challenged:
            challenged = params[0]

            # TODO: Use slack API to check if user exists
            if challenged.startswith('@'):
                challenged = params[0][1:]
            else:
                return web.HTTPBadRequest(text='You must challenge gnubg or an existing slack user (e.g. @austin)')

        worker = GnubgWorker()
        await worker.start(self.executable_path)
        self.workers[(user_name, challenged)] = worker

        _ = await worker.command('set player 1 name {}'.format(user_name))

        # set player name if opponent is human
        if challenged != 'gnubg':
            _ = await worker.command('set player 0 human')
            _ = await worker.command('set player 0 name {}'.format(challenged))

        gnubg_output = await worker.command('new game')

        slack_out = SlackTemplate.NewGame.format(
            challenger=user_name,
            challenged=challenged,
            board=''.join(gnubg_output),
        )
        await self.webhook.post(slack_out, channel=slack_params['channel_id'])

        return web.Response()

    @game_required()
    @run_command
    def move(): pass

    @game_required()
    @run_command
    def double(): pass

    @game_required()
    @run_command
    def roll(): pass

    @game_required(turn_required=False)
    @run_command
    def accept(): pass

    @game_required(turn_required=False)
    @run_command
    def redouble(): pass

    @game_required(turn_required=False)
    @run_command
    def reject(): pass

    @game_required()
    @run_command
    def resign(): pass

    @game_required(turn_required=False)
    async def quit(self, params, slack_params, player, opponent, worker):
        await worker.quit()

        key = (player, opponent) if (player, opponent) in self.workers else (opponent, player)
        del self.workers[key]

        slack_out = SlackTemplate.Quit.format(
            quitter=player,
            opponent=opponent,
        )
        await self.webhook.post(slack_out, channel=slack_params['channel_id'])

        return web.Response()


class IncomingWebhook:
    def __init__(self, url):
        self.url = url

    async def post(self, message, channel='#backgammon'):
        data = {
            'payload': json.dumps({
                'text': message,
                'channel': channel,
                'username': 'slackgammon',
                'icon_emoji': ':bg:',
            })
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(self.url, data=data) as resp:
                if resp.status != 200:
                    print("{}: {}".format(resp.status, await resp.text()))


REQUIRED_SLACK_PARAMS = [
    'user_id',
    'user_name',
    'channel_id',
]


async def slackgammon(request):
    app = request.app
    config = app['config']
    values = await request.post()

    if 'token' not in values or values['token'] != config.slash_token:
        return web.HTTPForbidden(text='Missing or invalid token.')

    slack_params = {}
    for k in REQUIRED_SLACK_PARAMS:
        if k not in values:
            return web.HTTPBadRequest(text='Missing required Slack parameter: {}'.format(k))
        slack_params[k] = values[k]

    gnubg_params = values.get('text', '').split()
    if not gnubg_params:
        return web.HTTPBadRequest(text='No command provided.')

    gnubg_command = gnubg_params[0]
    if gnubg_command not in GnubgManager.COMMANDS:
        return web.HTTPBadRequest(text='Invalid command.')

    gnubg_manager = app['manager']
    return await getattr(gnubg_manager, gnubg_command)(gnubg_params[1:], slack_params)


def main():
    parser = argparse.ArgumentParser(description='Slack frontend for GNU Backgammon',
                                     formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument('--host', type=str, default='localhost',
                        help='Host')
    parser.add_argument('--port', type=int, default=80,
                        help='Port')
    parser.add_argument('--slash-token', type=str, required=True,
                        help='Slack token for associated Slack slash command')
    parser.add_argument('--webhook-url', type=str, required=True,
                        help='Slack Incoming Webhook URL')
    parser.add_argument('--max-games', type=int, default=1,
                        help='Max instances of gnubg running to handle games')
    parser.add_argument('--gnubg-path', type=str, default='/usr/local/bin/gnubg',
                        help='Path for gnubg executable')

    loop = asyncio.get_event_loop()
    app = web.Application(loop=loop)

    config = parser.parse_args()
    app['config'] = config

    webhook = IncomingWebhook(config.webhook_url)
    manager = GnubgManager(config.gnubg_path, config.max_games, webhook)
    app['manager'] = manager

    app.router.add_post('/slackgammon', slackgammon, name='slackgammon')

    web.run_app(app, host=config.host, port=config.port)


if __name__ == '__main__':
    main()

