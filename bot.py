#!/usr/bin/env python

"""

Usage:
  bot.py [options]
  bot.py [options] load <path-to-words-file>
  bot.py [options] wipe
  bot.py [options] run

Options:
  -h --help     Show help
  -e --environment=<env-key>  izaber.yaml Environment to use [default: default]
  --db=<words url>   Path to words database file [default: sqlite:///data/db.sqlite]
  --debug            Enable debug messaging

"""

VERSION = '1.0'

IHA_COMMANDS = """
Usage:
  @iha [options]
  @iha help
  @iha add [options]
  @iha info [options]
  @iha remove [options]
  @iha sync [options]
  @iha start [options]
  @iha end [options]
  @iha rules [options]

Options:
  -h --help     Show help

"""

# Need:
# - Send Messages
# - Manage Messages
# - Read Message History
# - Use external emoji
# - Add Reactions
#     = 337984
# https://discord.com/api/oauth2/authorize?client_id=791729943725735987&permissions=337984&scope=bot

EMOJI_NAY = '🚫'

import re
import sys
import time
import logging
import datetime

from izaber import initialize, config

import shlex

import pprint
import docopt
import peewee as pw
from playhouse.db_url import connect

import discord
from discord.ext import commands

database_proxy = pw.DatabaseProxy()

def model_to_dict(model_rec):
    """ Using playhouse.shortcuts.model_to_dict()
    """
    dict_rec = playhouse.shortcuts.model_to_dict(model_rec)

    # Flatten any references
    for k in list(dict_rec.keys()):
        if k[0] == '_':
            del dict_rec[k]
            new_k = k[1:]
            dict_rec[new_k] = getattr(model_rec,new_k)

    # And strip out any foreign keys to other models
    for k,v in list(dict_rec.items()):
        if isinstance(v, pw.Model):
            del dict_rec[k]

class BaseModel(pw.Model):
    def as_dict(self):
        return model_to_dict(self)

    class Meta:
        database = database_proxy

class Words(BaseModel):
    id = pw.BigAutoField()
    word = pw.CharField(unique=True)

class Channels(BaseModel):
    id = pw.BigAutoField()
    discord_id = pw.BigIntegerField()
    name = pw.CharField(null=True)
    current_game = pw.DeferredForeignKey('Games', null=True)
    game_running = pw.BooleanField(default=False)

class Games(BaseModel):
    id = pw.BigAutoField()
    timestamp = pw.DateTimeField(index=True)
    channel = pw.ForeignKeyField(Channels, backref='games', on_delete='CASCADE', index=True)

class User(BaseModel):
    id = pw.BigAutoField()
    user_id = pw.BigIntegerField(unique=True)
    name = pw.CharField()

class Messages(BaseModel):
    id = pw.BigAutoField()
    timestamp = pw.DateTimeField(index=True)
    content = pw.TextField()
    word = pw.ForeignKeyField(Words, backref='messages', on_delete='CASCADE', index=True)
    game = pw.ForeignKeyField(Games, backref='game', on_delete='CASCADE', index=True, null=True)
    channel = pw.ForeignKeyField(Channels, backref='messages', on_delete='CASCADE', index=True)
    # author
    # message

    # <Message id=791740200609906689
    #   channel=<TextChannel id=791740186526351360
    #                        name='shiritori' 
    #                        position=2 nsfw=False 
    #                        news=False category_id=501079790086914050>
    #   type=<MessageType.default: 0>
    #   flags=<MessageFlags
    #   value=0>>


MODELS = [ Words, Channels, Games, Messages ]

class Iha(discord.Client):

    _db = None
    _channel_cache = None
    _word_cache = None

    def __init__(self, db_url, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._word_cache = {}
        self._channel_cache = {}
        self.db_conn(db_url)
        self.init()

    def init(self):
        """ Catches up on the most recent datasets
        """

    def db_conn(self, db_url):
        """
        """
        if not self._db:
            database = connect(db_url)
            database_proxy.initialize(database)
            database.create_tables(MODELS)
        return self._db

    def parse_message(self, clean_content):
        """ Parse the message string

        We expect the message to be in the form:

        word
        word word...
        words - comment
        words (comment)
        """

        elements = re.split('([ \(:!?])', clean_content.lower())
        word = []
        for element in elements:
            if element == ' ': continue
            if not re.search(r'^[a-z]',element):
                break
            word.append(element)
        word = " ".join(word)
        return word

    def is_game_channel(self, channel):
        """ Returns a truthy value if the channel is one that we
            consider a part of the game
        """
        discord_id = channel.id
        if discord_id in self._channel_cache:
            return self._channel_cache[discord_id]

        # We don't have a record, let's just double check the
        # tables
        try:
            # Get information about channel based upon the discord ID
            chan = Channels.get(discord_id = channel.id)
            self._channel_cache[discord_id] = chan

        except pw.DoesNotExist:
            self._channel_cache[discord_id] = None

        return self._channel_cache[discord_id]
        

    def word_upsert(self, word):
        """ Returns the database id for the word provided.
            If word is not found in the database, will create it
            and return the new id
        """

        # Just normalize it. Shouldn't be required but
        # just in case.
        word = word.lower().strip()

        if not word in self._word_cache:
            ( word_rec, created ) = Words.get_or_create(word = word)
            self._word_cache[word] = word_rec.id

        return self._word_cache[word]

    async def channel_sync(self, channel):
        # What's the newest entry we've got in the database?
        discord_id = channel.id
        channel_rec = Channels.get(discord_id == channel.id)
        channel_id = channel_rec.id

        max_timestamp = Messages.select(pw.fn.MAX(Messages.timestamp))\
                                .where(Messages.channel == channel_id)\
                                .scalar()

        chan = await self.fetch_channel(discord_id)
        async for message in chan.history(limit=None, after=max_timestamp):
            word = self.parse_message(message.clean_content)
            if not word:
                continue

            word_id = self.word_upsert(word)
            message = Messages.create(
                          timestamp=message.created_at,
                          content=message.clean_content,
                          word=word_id,
                          channel=channel_id,
                      )
            print(message, word_id, word)


    async def channel_add(self, channel):
        """ Loads a channel into the system
        """
        ( chan_rec, created ) = Channels.get_or_create(discord_id = channel.id)
        if chan_rec.name != channel.name:
            chan_rec.name = channel.name
            chan_rec.save()

        self._channel_cache[channel.id] = chan_rec

        # Load the history
        await self.channel_sync(channel)

    async def channel_info(self, channel):
        """ Reports information associated with the channel
        """
        try:
            # Get information about channel based upon the discord ID
            chan = Channels.get(discord_id = channel.id)

            # How many messages have we logged in this channel?
            messages = Messages.select(Messages.channel == chan).count()

            # Summarize the discovered information if available
            data = {
                'discord_id': chan.discord_id,
                'name': chan.name,
                'messages': messages,
                'game_running': chan.game_running,
            }

            return data
        except pw.DoesNotExist:
            return None

    async def command_help(self, args, message):
        """ Respond to help command by responding with the invocation syntax
        """
        await message.channel.send(f"```\n{IHA_COMMANDS}\n```")

    async def channel_remove(self, channel):
        """ Removes the channel information from the database
        """
        self._channel_cache[channel.id] = None
        try:
            # Get information about channel based upon the discord ID
            Channels.get(discord_id = channel.id).delete_instance()
            return True
        except pw.DoesNotExist:
            return False

    async def game_start(self, channel):
        """ Starts a new game in the current channel. If a game is already active,
            will throw back a warning message
        """

        # Check if this is a game channel
        if not self.is_game_channel(channel):
            raise Exception("Not a game channel")

        # Check to see if the game is in session
        channel_rec = Channels.get(Channels.discord_id == channel.id)
        if channel_rec.game_running:
            raise Exception(f"Game is already running in {channel_rec.name}")

        # Launch a new game by creating a new instance in Game
        game = Games.create(
                    timestamp = datetime.datetime.now(),
                    channel = channel_rec,
                )
        channel_rec.current_game = game
        channel_rec.game_running = True
        channel_rec.save()

        return game

    async def game_ending(self, channel):
        """ Ends a game in the current channel. If no game is active, will throw back
            a warning message
        """

        # Check if this is a game channel
        if not self.is_game_channel(channel):
            raise Exception("Not a game channel")

        # Ensure that a game is in session
        channel_rec = Channels.get(Channels.discord_id == channel.id)
        if not channel_rec.game_running:
            raise Exception(f"No game is currently running in {channel_rec.name}")

        # Launch a new game by creating a new instance in Game
        game_rec = channel_rec.current_game
        channel_rec.current_game = None
        channel_rec.game_running = False
        channel_rec.save()

        # game_rec returned holds a reference to the ended game
        return game_rec

    async def command_execute(self, message):
        """ Invoke a bot command
        """

        # If we were invoked via mention, let's remove it
        try:
            command = message.content
            argv = shlex.split(message.clean_content)
            if not argv:
                raise Exception("Syntax Error")

            if argv[0].lower() != '@iha':
                raise Exception("Unknown Mention")

            args = docopt.docopt(
                        doc=IHA_COMMANDS,
                        help=False,
                        version=VERSION,
                        argv=argv[1:],
                    )

            if args['--help'] or args['help']:
                await self.command_help(args, message)

            # Channel Commands
            elif args['add']:
                await self.channel_add(message.channel)
                await message.channel.send(f":white_check_mark: **{message.channel.name}** Added.")
            elif args['sync']:
                await self.channel_sync(message.channel)
                await message.channel.send(f":white_check_mark: **{message.channel.name}** Syncronized")
            elif args['info']:
                info = await self.channel_info(message.channel)
                if info:
                    await message.channel.send(
                        f"**{info['name']}** is registered. "\
                        f"Currently logged {info['messages']} message(s)."
                    )
                    if info['game_running']:
                        await message.channel.send(f"Currently in game. Started {info['game_running'].timestamp}!")
                    else:
                        await message.channel.send(f"No game in session.")
                else:
                    await message.channel.send(f"**{message.channel.name}** is not a registered channel.")
            elif args['remove']:
                await self.channel_remove(message.channel)

            # Game commands
            elif args['start']:
                await self.game_start(message.channel)
                await message.channel.send(f"**{message.channel.name}** game started.")

            elif args['end']:
                await self.game_end(message.channel)
                await message.channel.send(f"**{message.channel.name}** game ended.")

            pprint.pprint(args)
        except docopt.DocoptExit as ex:
            await message.channel.send(f"Parser Error: {ex}")
        except Exception as ex:
            await message.channel.send(f"Internal Error: {ex}")

    async def on_ready(self):
        print('Logged on as {0}!'.format(self.user))

        """
        for chan_id in SHIRITORI_CHANNELS:
            chan = await self.fetch_channel(chan_id)
            async for message in chan.history(limit=None):
                print(message)
        """

    async def on_message(self, message):
        print(message.channel.id)
        print('Message from {0.author}: {0.content}'.format(message))
        print("????", message.author, self.user)
        if message.author == self.user:
            return

        # Are we being asked to do something?
        if self.user.id in message.raw_mentions:
            return await self.command_execute(message)

        # Don't need to do anything if this is a message in a channel
        # we don't care about
        if not self.is_game_channel(message.channel):
            return

        # Process shiritori?
        print("GONNA RESPOND", message.channel.id)
        """
        if message.channel.id in SHIRITORI_CHANNELS:
            await message.add_reaction(EMOJI_NAY)
            #await message.add_reaction('<:science:792107970703261717>')
            #emojis = self.get_all_emojis()
            #await message.channel.send("Something!")
        """

    async def on_reaction_add(self, reaction, user):
        print(f"Reaction from {user}: {reaction}")

    async def on_reaction_remove(self, reaction, user):
        print(f"Reaction Removed by {user}: {reaction}")


def do_load(args):
    db = args['--db']
    database = connect(db)
    database_proxy.initialize(database)
    database.create_tables(MODELS)

    words_fpath = args['<path-to-words-file>']
    with open(words_fpath,'r') as f:
        while True:
            words = []
            for l in f:
                words.append({
                  'word': l.strip()
                })
                if len(words) > 1000:
                    break
            if not words:
                break
            Words.insert_many(words).execute()
    database.commit()

def do_wipe(args):
    db = args['--db']
    database = connect(db)
    database_proxy.initialize(database)
    database.drop_tables(MODELS)

def do_run(args):
    db = args['--db']
    intents = discord.Intents().default()
    client = Iha(db,intents=intents)
    client.run(config.discord.mixerbot.key)

def main(args):
    initialize('mixerbot')
    if args['load']:
        return do_load(args)
    elif args['wipe']:
        return do_wipe(args)
    elif args['run']:
        return do_run(args)



if __name__ == '__main__':
    args = docopt.docopt(__doc__, version=VERSION)
    if args['--debug']:
        logging.basicConfig(stream=sys.stdout, level=1)
    main(args)
