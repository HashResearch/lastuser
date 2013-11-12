import os
from datetime import datetime, timedelta
from functools import wraps
from urllib import unquote
from pytz import common_timezones
from flask import g, current_app, request, session, flash, redirect, url_for, Response
from coaster.views import get_current_url
from lastuser_core.models import db, User, Client
from lastuser_core.signals import user_login, user_logout, user_registered
from .. import lastuser_oauth

valid_timezones = set(common_timezones)


@lastuser_oauth.before_app_request
def lookup_current_user():
    """
    If there's a userid in the session, retrieve the user object and add
    to the request namespace object g.
    """
    g.user = None
    if 'userid' in session:
        g.user = User.get(userid=session['userid'])


@lastuser_oauth.after_app_request
def cache_expiry_headers(response):
    if 'Expires' not in response.headers:
        response.headers['Expires'] = 'Fri, 01 Jan 1990 00:00:00 GMT'
    if 'Cache-Control' in response.headers:
        if 'private' not in response.headers['Cache-Control']:
            response.headers['Cache-Control'] = 'private, ' + response.headers['Cache-Control']
    else:
        response.headers['Cache-Control'] = 'private'
    return response


@lastuser_oauth.app_template_filter('usessl')
def usessl(url):
    """
    Convert a URL to https:// if SSL is enabled in site config
    """
    if not current_app.config.get('USE_SSL'):
        return url
    if url.startswith('//'):  # //www.example.com/path
        return 'https:' + url
    if url.startswith('/'):  # /path
        url = os.path.join(request.url_root, url[1:])
    if url.startswith('http:'):  # http://www.example.com
        url = 'https:' + url[5:]
    return url


@lastuser_oauth.app_template_filter('nossl')
def nossl(url):
    """
    Convert a URL to http:// if using SSL
    """
    if url.startswith('//'):
        return 'http:' + url
    if url.startswith('/') and request.url.startswith('https:'):  # /path and SSL is on
        url = os.path.join(request.url_root, url[1:])
    if url.startswith('https://'):
        return 'http:' + url[6:]
    return url


def requires_login(f):
    """
    Decorator to require a login for the given view.
    """
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if g.user is None:
            flash(u"You need to be logged in for that page", "info")
            session['next'] = get_current_url()
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function


def requires_login_no_message(f):
    """
    Decorator to require a login for the given view.
    """
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if g.user is None:
            session['next'] = get_current_url()
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function


def _client_login_inner():
    if request.authorization is None:
        return Response(u"Client credentials required.", 401,
            {'WWW-Authenticate': 'Basic realm="Client credentials"'})
    client = Client.get(key=request.authorization.username)
    if client is None or not client.secret_is(request.authorization.password):
        return Response(u"Invalid client credentials.", 401,
            {'WWW-Authenticate': 'Basic realm="Client credentials"'})
    g.client = client


def requires_client_login(f):
    """
    Decorator to require a client login via HTTP Basic Authorization.
    """
    @wraps(f)
    def decorated_function(*args, **kwargs):
        result = _client_login_inner()
        if result is None:
            return f(*args, **kwargs)
        else:
            return result
    return decorated_function


def requires_user_or_client_login(f):
    """
    Decorator to require a user or client login (user by cookie, client by HTTP Basic).
    """
    @wraps(f)
    def decorated_function(*args, **kwargs):
        # Check for user first:
        if g.user is not None:
            return f(*args, **kwargs)
        # If user is not logged in, check for client
        result = _client_login_inner()
        if result is None:
            return f(*args, **kwargs)
        else:
            return result
    return decorated_function


def login_internal(user):
    g.user = user
    session['userid'] = user.userid
    session.permanent = True
    autoset_timezone(user)
    user_login.send(user)


def autoset_timezone(user):
    # Set the user's timezone automatically if available
    if user.timezone is None or user.timezone not in valid_timezones:
        if request.cookies.get('timezone'):
            timezone = unquote(request.cookies.get('timezone'))
            if timezone in valid_timezones:
                user.timezone = timezone


def logout_internal():
    user = g.user
    g.user = None
    session.pop('userid', None)
    session.pop('merge_userid', None)
    session.pop('userid_external', None)
    session.pop('avatar_url', None)
    session.permanent = False
    if user is not None:
        user_logout.send(user)


def register_internal(username, fullname, password):
    user = User(username=username, fullname=fullname, password=password)
    if not username:
        user.username = None
    db.session.add(user)
    user_registered.send(user)
    return user


def set_loginmethod_cookie(response, value):
    response.set_cookie('login', value, max_age=31557600,  # Keep this cookie for a year
        expires=datetime.utcnow() + timedelta(days=365))   # Expire one year from now
    return response
