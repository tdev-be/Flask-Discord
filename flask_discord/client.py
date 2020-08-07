from typing import Union

from . import configs, _http, models

from flask import request, session, redirect
from oauthlib.common import add_params_to_uri, generate_token
import discord
import jwt


class DiscordOAuth2Session(_http.DiscordOAuth2HttpClient):
    """Main client class representing hypothetical OAuth2 session with discord.
    It uses Flask `session <http://flask.pocoo.org/docs/1.0/api/#flask.session>`_ local proxy object
    to save state, authorization token and keeps record of users sessions across different requests.
    This class inherits :py:class:`flask_discord._http.DiscordOAuth2HttpClient` class.

    Parameters
    ----------
    `app` : Flask
        An instance of your `flask application <http://flask.pocoo.org/docs/1.0/api/#flask.Flask>`_.
    `client_id` : int, optional
        The client ID of discord application provided. Can be also set to flask config
        with key ``DISCORD_CLIENT_ID``.
    `client_secret` : str, optional
        The client secret of discord application provided. Can be also set to flask config
        with key ``DISCORD_CLIENT_SECRET``.
    `redirect_uri` : str, optional
        The default URL to use to redirect user to after authorization. Can be also set to flask config
        with key ``DISCORD_REDIRECT_URI``.
    `bot_token` : str, optional
        The bot token of the application. This is required when you also need to access bot scope resources
        beyond the normal resources provided by the OAuth. Can be also set to flask config with
        key ``DISCORD_BOT_TOKEN``.
    `users_cache` : cachetools.LFUCache, optional
        Any dict like mapping to internally cache the authorized users. Preferably an instance of
        cachetools.LFUCache or cachetools.TTLCache. If not specified, default cachetools.LFUCache is used.
        Uses the default max limit for cache if ``DISCORD_USERS_CACHE_MAX_LIMIT`` isn't specified in app config.

    Attributes
    ----------
    `client_id` : int
        The client ID of discord application provided.
    `redirect_uri` : str
        The default URL to use to redirect user to after authorization.
    `users_cache` : cachetools.LFUCache
        A dict like mapping to internally cache the authorized users. Preferably an instance of
        cachetools.LFUCache or cachetools.TTLCache. If not specified, default cachetools.LFUCache is used.
        Uses the default max limit for cache if ``DISCORD_USERS_CACHE_MAX_LIMIT`` isn't specified in app config.

    """

    def create_session(self, scope: list = None, prompt: str = "consent",
                       permissions: Union[discord.Permissions, int] = None,
                       guild_id: int = None, disable_guild_select: bool = None,
                       **params):
        """Primary method used to create OAuth2 session and redirect users for
        authorization code grant.

        Parameters
        ----------
        scope : list, optional
            An optional list of valid `Discord OAuth2 Scopes
            <https://discordapp.com/developers/docs/topics/oauth2#shared-resources-oauth2-scopes>`_.
        prompt : str, optional
        permissions: discord.Permissions object or int, optional
        guild_id : int, optional
        disable_guild_select : bool, optional
        params : kwargs, optional
            An optional mapping of query parameters to supply to the authorization URL.
            Since query parameters aren't passed through Discord Oauth2, these get added to the state.
            Use `:py:meth:`flask_discord.DiscordOAuth2Session.callback()` to retrieve the params passed in.

        Notes
        -----
         `prompt` has been changed. You must specify the raw value ('consent' or 'none'). Defaults to 'consent'.

        Returns
        -------
        redirect
            Flask redirect to discord authorization servers to complete authorization code grant process.

        """
        scope = scope or request.args.get("scope", str()).split() or configs.DISCORD_OAUTH_DEFAULT_SCOPES

        if prompt != "consent" and set(scope) & set(configs.DISCORD_PASSTHROUGH_SCOPES):
            raise ValueError("You should use explicit OAuth grant for passthrough scopes like bot.")

        if permissions is not None and not (isinstance(permissions, discord.Permissions)
                                            or isinstance(permissions, int)):
            raise ValueError(f"permissions must be an int or discord.Permissions, not {type(permissions)}.")

        if isinstance(permissions, discord.Permissions):
            permissions = permissions.value

        # Encode any params into a jwt with the state as the key
        # Use generate_token in case state is None
        session['DISCORD_RAW_OAUTH2_STATE'] = session.get("DISCORD_OAUTH2_STATE", generate_token())
        state = jwt.encode(params, session.get("DISCORD_RAW_OAUTH2_STATE"))

        discord_session = self._make_session(scope=scope, state=state)
        authorization_url, state = discord_session.authorization_url(configs.DISCORD_AUTHORIZATION_BASE_URL)

        # Save the encoded state as that's what Oauth2 lib is expecting
        session["DISCORD_OAUTH2_STATE"] = state.decode("utf-8")

        # Add special parameters to uri instead of state
        uri_params = {'prompt': prompt}
        if permissions:
            uri_params.update(permissions=permissions)
        if guild_id:
            uri_params.update(guild_id=guild_id)
        if disable_guild_select is not None:
            uri_params.update(disable_guild_select=disable_guild_select)

        authorization_url = add_params_to_uri(authorization_url, uri_params)
        if permissions:
            authorization_url = add_params_to_uri(authorization_url, {'permissions': permissions})

        return redirect(authorization_url)

    @staticmethod
    def save_authorization_token(token: dict):
        """A staticmethod which saves a dict containing Discord OAuth2 token and other secrets to the user's cookies.
        Meaning by default, it uses client side session handling.

        Override this method if you want to handle the user's session server side. If this method is overridden then,
        you must also override :py:meth:`flask_discord.DiscordOAuth2Session.get_authorization_token`.

        """
        session["DISCORD_OAUTH2_TOKEN"] = token

    @staticmethod
    def get_authorization_token() -> dict:
        """A static method which returns a dict containing Discord OAuth2 token and other secrets which was saved
        previously by `:py:meth:`flask_discord.DiscordOAuth2Session.save_authorization_token` from user's cookies.

        You must override this method if you are implementing server side session handling.

        """
        return session.get("DISCORD_OAUTH2_TOKEN")

    def callback(self):
        """A method which should be always called after completing authorization code grant process
        usually in callback view.
        It fetches the authorization token and saves it flask
        `session <http://flask.pocoo.org/docs/1.0/api/#flask.session>`_ object.

        """
        if request.values.get("error"):
            return request.values["error"]
        token = self._fetch_token()
        self.save_authorization_token(token)

        # Decode any parameters passed through state variable
        raw_oauth_state = session.get("DISCORD_RAW_OAUTH2_STATE")
        passed_state = request.args.get("state")
        return jwt.decode(passed_state, raw_oauth_state)

    def revoke(self):
        """This method clears current discord token, state and all session data from flask
        `session <http://flask.pocoo.org/docs/1.0/api/#flask.session>`_. Which means user will have
        to go through discord authorization token grant flow again. Also tries to remove the user from internal
        cache if they exist.

        """

        self.users_cache.pop(self.user_id, None)

        for session_key in self.SESSION_KEYS:
            try:
                session.pop(session_key)
            except KeyError:
                pass

    @property
    def authorized(self):
        """A boolean indicating whether current session has authorization token or not."""
        return self._make_session().authorized

    @staticmethod
    def fetch_user() -> models.User:
        """This method returns user object from the internal cache if it exists otherwise makes an API call to do so.

        Returns
        -------
        flask_discord.models.User

        """
        return models.User.get_from_cache() or models.User.fetch_from_api()

    @staticmethod
    def fetch_connections() -> list:
        """This method returns list of user connection objects from internal cache if it exists otherwise
        makes an API call to do so.

        Returns
        -------
        list
            List of :py:class:`flask_discord.models.UserConnection` objects.

        """
        user = models.User.get_from_cache()
        try:
            if user.connections is not None:
                return user.connections
        except AttributeError:
            pass

        return models.UserConnection.fetch_from_api()

    @staticmethod
    def fetch_guilds() -> list:
        """This method returns list of guild objects from internal cache if it exists otherwise makes an API
        call to do so.

        Returns
        -------
        list
            List of :py:class:`flask_discord.models.Guild` objects.

        """
        user = models.User.get_from_cache()
        try:
            if user.guilds is not None:
                return user.guilds
        except AttributeError:
            pass

        return models.Guild.fetch_from_api()
