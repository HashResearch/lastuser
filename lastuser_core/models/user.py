# -*- coding: utf-8 -*-

from hashlib import md5
from werkzeug import check_password_hash, cached_property
import bcrypt
from sqlalchemy.ext.hybrid import hybrid_property
from coaster import newid, newsecret, newpin, valid_username

from . import db, TimestampMixin, BaseMixin


__all__ = ['User', 'UserEmail', 'UserEmailClaim', 'PasswordResetRequest', 'UserExternalId',
           'UserPhone', 'UserPhoneClaim', 'Team', 'Organization', 'UserOldId', 'USER_STATUS', 'UserPastEmail']


class USER_STATUS:
    ACTIVE = 0
    SUSPENDED = 1
    MERGED = 2


class User(BaseMixin, db.Model):
    __tablename__ = 'user'
    __bind_key__ = 'lastuser'
    userid = db.Column(db.String(22), unique=True, nullable=False, default=newid)
    fullname = db.Column(db.Unicode(80), default=u'', nullable=False)
    _username = db.Column('username', db.Unicode(80), unique=True, nullable=True)
    pw_hash = db.Column(db.String(80), nullable=True)
    timezone = db.Column(db.Unicode(40), nullable=True)
    description = db.Column(db.UnicodeText, default=u'', nullable=False)
    status = db.Column(db.SmallInteger, nullable=False, default=USER_STATUS.ACTIVE)

    def __init__(self, password=None, **kwargs):
        self.userid = newid()
        self.password = password
        super(User, self).__init__(**kwargs)

    @property
    def is_active(self):
        return self.status == USER_STATUS.ACTIVE

    def _set_password(self, password):
        if password is None:
            self.pw_hash = None
        else:
            self.pw_hash = bcrypt.hashpw(
                password.encode('utf-8') if isinstance(password, unicode) else password,
                bcrypt.gensalt())

    #: Write-only property (passwords cannot be read back in plain text)
    password = property(fset=_set_password)

    #: Username (may be null)
    @hybrid_property
    def username(self):
        return self._username

    @username.setter
    def username(self, value):
        if not value:
            self._username = None
        elif self.is_valid_username(value):
            self._username = value

    def is_valid_username(self, value):
        if not valid_username(value):
            return False
        existing = User.query.filter(db.or_(
            User.username == value,
            User.userid == value)).first()  # Avoid User.get to skip status check
        if existing and existing.id != self.id:
            return False
        existing = Organization.get(name=value)
        if existing:
            return False
        return True

    def password_is(self, password):
        if self.pw_hash is None:
            return False
        if self.pw_hash.startswith('sha1$'):
            return check_password_hash(self.pw_hash, password)
        else:
            return bcrypt.hashpw(
                password.encode('utf-8') if isinstance(password, unicode) else password,
                self.pw_hash) == self.pw_hash

    def __repr__(self):
        return u'<User {username} "{fullname}">'.format(username=self.username or self.userid,
            fullname=self.fullname)

    def profileid(self):
        if self.username:
            return self.username
        else:
            return self.userid

    def displayname(self):
        return self.fullname or self.username or self.userid

    @property
    def pickername(self):
        if self.username:
            return u'{fullname} (~{username})'.format(fullname=self.fullname, username=self.username)
        else:
            return self.fullname

    def add_email(self, email, primary=False):
        if primary:
            for emailob in self.emails:
                if emailob.primary:
                    emailob.primary = False
        useremail = UserEmail(user=self, email=email, primary=primary)
        db.session.add(useremail)
        return useremail

    def del_email(self, email):
        setprimary = False
        useremail = UserEmail.query.filter_by(user=self, email=email).first()
        if useremail:
            if useremail.primary:
                setprimary = True
            user_past_email = UserPastEmail(user=self, email=useremail.email)
            db.session.add(user_past_email)
            db.session.delete(useremail)
        if setprimary:
            for emailob in UserEmail.query.filter_by(user=self).all():
                if emailob is not useremail:
                    emailob.primary = True
                    break

    @cached_property
    def email(self):
        """
        Returns primary email address for user.
        """
        # Look for a primary address
        useremail = UserEmail.query.filter_by(user_id=self.id, primary=True).first()
        if useremail:
            return useremail
        # No primary? Maybe there's one that's not set as primary?
        useremail = UserEmail.query.filter_by(user_id=self.id).first()
        if useremail:
            # XXX: Mark at primary. This may or may not be saved depending on
            # whether the request ended in a database commit.
            useremail.primary = True
            return useremail
        # This user has no email address. Return a blank string instead of None
        # to support the common use case, where the caller will use unicode(user.email)
        # to get the email address as a string.
        return u''

    @cached_property
    def phone(self):
        """
        Returns primary phone number for user.
        """
        # Look for a primary address
        userphone = UserPhone.query.filter_by(user=self, primary=True).first()
        if userphone:
            return userphone
        # No primary? Maybe there's one that's not set as primary?
        userphone = UserPhone.query.filter_by(user=self).first()
        if userphone:
            # XXX: Mark at primary. This may or may not be saved depending on
            # whether the request ended in a database commit.
            userphone.primary = True
            return userphone
        # This user has no phone number. Return a blank string instead of None
        # to support the common use case, where the caller will use unicode(user.phone)
        # to get the phone number as a string.
        return u''

    def organizations(self):
        """
        Return the organizations this user is a member of.
        """
        return sorted(set([team.org for team in self.teams]), key=lambda o: o.title)

    def organizations_owned(self):
        """
        Return the organizations this user is an owner of.
        """
        return sorted(set([team.org for team in self.teams if team.org.owners == team]),
            key=lambda o: o.title)

    def organizations_owned_ids(self):
        """
        Return the database ids of the organizations this user is an owner of. This is used
        for database queries.
        """
        return list(set([team.org.id for team in self.teams if team.org.owners == team]))

    def is_profile_complete(self):
        """
        Return True if profile is complete (fullname, username and email are present), False
        otherwise.
        """
        return bool(self.fullname and self.username and self.email)

    @classmethod
    def get(cls, username=None, userid=None):
        """
        Return a User with the given username or userid.

        :param str username: Username to lookup
        :param str userid: Userid to lookup
        """
        if not bool(username) ^ bool(userid):
            raise TypeError("Either username or userid should be specified")

        if userid:
            return cls.query.filter_by(userid=userid, status=USER_STATUS.ACTIVE).first()
        else:
            return cls.query.filter_by(username=username, status=USER_STATUS.ACTIVE).first()

    def available_permissions(self):
        """
        Return all permission objects available to this user
        (either owned by user or available to all users).
        """
        from .client import Permission
        return Permission.query.filter(
            db.or_(Permission.allusers == True, Permission.user == self)
            ).order_by(Permission.name).all()


class UserOldId(TimestampMixin, db.Model):
    __tablename__ = 'useroldid'
    __bind_key__ = 'lastuser'
    userid = db.Column(db.String(22), nullable=False, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    user = db.relationship(User, primaryjoin=user_id == User.id,
        backref=db.backref('oldids', cascade="all, delete-orphan"))


class UserEmail(BaseMixin, db.Model):
    __tablename__ = 'useremail'
    __bind_key__ = 'lastuser'
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    user = db.relationship(User, primaryjoin=user_id == User.id,
        backref=db.backref('emails', cascade="all, delete-orphan"))
    _email = db.Column('email', db.Unicode(80), unique=True, nullable=False)
    md5sum = db.Column(db.String(32), unique=True, nullable=False)
    primary = db.Column(db.Boolean, nullable=False, default=False)

    def __init__(self, email, **kwargs):
        super(UserEmail, self).__init__(**kwargs)
        self._email = email
        self.md5sum = md5(self._email).hexdigest()

    @hybrid_property
    def email(self):
        return self._email

    #: Make email immutable. There is no setter for email.
    email = db.synonym('_email', descriptor=email)

    def __repr__(self):
        return u'<UserEmail {email} of user {user}>'.format(email=self.email, user=repr(self.user))

    def __unicode__(self):
        return unicode(self.email)

    def __str__(self):
        return str(self.__unicode__())

    @classmethod
    def get(cls, email=None, md5sum=None):
        """
        Return a UserEmail with matching email or md5sum.

        :param str email: Email address to lookup
        :param str md5sum: md5sum of email address to lookup
        """
        if not bool(email) ^ bool(md5sum):
            raise TypeError("Either email or md5sum should be specified")

        if email:
            return cls.query.filter(cls.email.in_([email, email.lower()])).first()
        else:
            return cls.query.filter_by(md5sum=md5sum).first()


class UserPastEmail(BaseMixin, db.Model):
    __tablename__ = 'user_past_email'
    __bind_key__ = 'lastuser'
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    user = db.relationship(User, primaryjoin=user_id == User.id,
        backref=db.backref('past_emails', cascade="all, delete-orphan"))
    _email = db.Column('email', db.Unicode(80), nullable=False)

    def __init__(self, email, **kwargs):
        super(UserPastEmail, self).__init__(**kwargs)
        self._email = email

    @property
    def email(self):
        return self._email

    #: Make email immutable. There is no setter for email.
    email = db.synonym('_email', descriptor=email)


class UserEmailClaim(BaseMixin, db.Model):
    __tablename__ = 'useremailclaim'
    __bind_key__ = 'lastuser'
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    user = db.relationship(User, primaryjoin=user_id == User.id,
        backref=db.backref('emailclaims', cascade="all, delete-orphan"))
    _email = db.Column('email', db.Unicode(80), nullable=True)
    verification_code = db.Column(db.String(44), nullable=False, default=newsecret)
    md5sum = db.Column(db.String(32), nullable=False)

    def __init__(self, email, **kwargs):
        super(UserEmailClaim, self).__init__(**kwargs)
        self.verification_code = newsecret()
        self._email = email
        self.md5sum = md5(self._email).hexdigest()

    @hybrid_property
    def email(self):
        return self._email

    #: Make email immutable. There is no setter for email.
    email = db.synonym('_email', descriptor=email)

    def __repr__(self):
        return u'<UserEmailClaim {email} of user {user}>'.format(email=self.email, user=repr(self.user))

    def __unicode__(self):
        return unicode(self.email)

    def __str__(self):
        return str(self.__unicode__())

    def permissions(self, user, inherited=None):
        perms = super(UserEmailClaim, self).permissions(user, inherited)
        if user and user == self.user:
            perms.add('verify')
        return perms

    @classmethod
    def get(cls, email, user):
        """
        Return a UserEmailClaim with matching email address for the given user.

        :param str email: Email address to lookup
        :param User user: User who claimed this email address
        """
        query = cls.query.filter(UserEmailClaim.email.in_([email, email.lower()])).filter_by(user=user).first()

    @classmethod
    def all(cls, email):
        """
        Return all UserEmailClaim instances with matching email address.

        :param str email: Email address to lookup
        """
        return cls.query.filter(UserEmailClaim.email.in_([email, email.lower()])).all()


class UserPhone(BaseMixin, db.Model):
    __tablename__ = 'userphone'
    __bind_key__ = 'lastuser'
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    user = db.relationship(User, primaryjoin=user_id == User.id,
        backref=db.backref('phones', cascade="all, delete-orphan"))
    primary = db.Column(db.Boolean, nullable=False, default=False)
    _phone = db.Column('phone', db.Unicode(80), unique=True, nullable=False)
    gets_text = db.Column(db.Boolean, nullable=False, default=True)

    def __init__(self, phone, **kwargs):
        super(UserPhone, self).__init__(**kwargs)
        self._phone = phone

    @hybrid_property
    def phone(self):
        return self._phone

    phone = db.synonym('_phone', descriptor=phone)

    def __repr__(self):
        return u'<UserPhone {phone} of user {user}>'.format(phone=self.phone, user=repr(self.user))

    def __unicode__(self):
        return unicode(self.phone)

    def __str__(self):
        return str(self.__unicode__())

    @classmethod
    def get(cls, phone):
        """
        Return a UserPhone with matching phone number.

        :param str phone: Phone number to lookup (must be an exact match)
        """
        return cls.query.filter_by(phone=phone).first()


class UserPhoneClaim(BaseMixin, db.Model):
    __tablename__ = 'userphoneclaim'
    __bind_key__ = 'lastuser'
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    user = db.relationship(User, primaryjoin=user_id == User.id,
        backref=db.backref('phoneclaims', cascade="all, delete-orphan"))
    _phone = db.Column('phone', db.Unicode(80), nullable=False)
    gets_text = db.Column(db.Boolean, nullable=False, default=True)
    verification_code = db.Column(db.Unicode(4), nullable=False, default=newpin)

    def __init__(self, phone, **kwargs):
        super(UserPhoneClaim, self).__init__(**kwargs)
        self.verification_code = newpin()
        self._phone = phone

    @hybrid_property
    def phone(self):
        return self._phone

    phone = db.synonym('_phone', descriptor=phone)

    def __repr__(self):
        return u'<UserPhoneClaim {phone} of user {user}>'.format(phone=self.phone, user=repr(self.user))

    def __unicode__(self):
        return unicode(self.phone)

    def __str__(self):
        return str(self.__unicode__())

    def permissions(self, user, inherited=None):
        perms = super(UserPhoneClaim, self).permissions(user, inherited)
        if user and user == self.user:
            perms.add('verify')
        return perms

    @classmethod
    def get(cls, phone, user):
        """
        Return a UserPhoneClaim with matching phone number for the given user.

        :param str phone: Phone number to lookup (must be an exact match)
        :param User user: User who claimed this phone number
        """
        return cls.query.filter_by(phone=phone, user=user).first()

    @classmethod
    def all(cls, phone):
        """
        Return all UserPhoneClaim instances with matching phone number.

        :param str phone: Phone number to lookup (must be an exact match)
        """
        return cls.query.filter_by(phone=phone).all()


class PasswordResetRequest(BaseMixin, db.Model):
    __tablename__ = 'passwordresetrequest'
    __bind_key__ = 'lastuser'
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    user = db.relationship(User, primaryjoin=user_id == User.id)
    reset_code = db.Column(db.String(44), nullable=False, default=newsecret)

    def __init__(self, **kwargs):
        super(PasswordResetRequest, self).__init__(**kwargs)
        self.reset_code = newsecret()


class UserExternalId(BaseMixin, db.Model):
    __tablename__ = 'userexternalid'
    __bind_key__ = 'lastuser'
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    user = db.relationship(User, primaryjoin=user_id == User.id,
        backref=db.backref('externalids', cascade="all, delete-orphan"))
    service = db.Column(db.String(20), nullable=False)
    userid = db.Column(db.String(250), nullable=False)  # Unique id (or OpenID)
    username = db.Column(db.Unicode(80), nullable=True)
    oauth_token = db.Column(db.String(250), nullable=True)
    oauth_token_secret = db.Column(db.String(250), nullable=True)
    oauth_token_type = db.Column(db.String(250), nullable=True)

    __table_args__ = (db.UniqueConstraint("service", "userid"), {})

    @classmethod
    def get(cls, service, userid=None, username=None):
        """
        Return a UserExternalId with the given service and userid or username.

        :param str service: Service to lookup
        :param str userid: Userid to lookup
        :param str username: Username to lookup (may be non-unique)

        Usernames are not guaranteed to be unique within a service. An example is with Google,
        where the userid is a directed OpenID URL, unique but subject to change if the Lastuser
        site URL changes. The username is the email address, which will be the same despite
        different userids.
        """
        if not bool(userid) ^ bool(username):
            raise TypeError("Either userid or username should be specified")

        if userid:
            return cls.query.filter_by(service=service, userid=userid).first()
        else:
            return cls.query.filter_by(service=service, username=username).first()

# --- Organizations and teams -------------------------------------------------


team_membership = db.Table(
    'team_membership', db.Model.metadata,
    db.Column('user_id', db.Integer, db.ForeignKey('user.id'), nullable=False),
    db.Column('team_id', db.Integer, db.ForeignKey('team.id'), nullable=False),
    info={'bind_key': 'lastuser'}
    )


class Organization(BaseMixin, db.Model):
    __tablename__ = 'organization'
    __bind_key__ = 'lastuser'
    # owners_id cannot be null, but must be declared with nullable=True since there is
    # a circular dependency. The post_update flag on the relationship tackles the circular
    # dependency within SQLAlchemy.
    owners_id = db.Column(db.Integer, db.ForeignKey('team.id',
        use_alter=True, name='fk_organization_owners_id'), nullable=True)
    owners = db.relationship('Team', primaryjoin='Organization.owners_id == Team.id',
        uselist=False, cascade='all', post_update=True)
    userid = db.Column(db.String(22), unique=True, nullable=False, default=newid)
    _name = db.Column('name', db.Unicode(80), unique=True, nullable=True)
    title = db.Column(db.Unicode(80), default=u'', nullable=False)
    description = db.Column(db.UnicodeText, default=u'', nullable=False)

    def __init__(self, *args, **kwargs):
        super(Organization, self).__init__(*args, **kwargs)
        if self.owners is None:
            self.owners = Team(title=u"Owners", org=self)

    @hybrid_property
    def name(self):
        return self._name

    @name.setter
    def name(self, value):
        if self.valid_name(value):
            self._name = value

    def valid_name(self, value):
        if not valid_username(value):
            return False
        existing = Organization.get(name=value)
        if existing and existing.id != self.id:
            return False
        existing = User.query.filter_by(username=value).first()  # Avoid User.get to skip status check
        if existing:
            return False
        return True

    def __repr__(self):
        return u'<Organization {name} "{title}">'.format(name=self.name or self.userid, title=self.title)

    @property
    def pickername(self):
        if self.name:
            return u'{title} (~{name})'.format(title=self.title, name=self.name)
        else:
            return self.title

    def clients_with_team_access(self):
        """
        Return a list of clients with access to the organization's teams.
        """
        from lastuser_core.models.client import CLIENT_TEAM_ACCESS
        return [cta.client for cta in self.client_team_access if cta.access_level == CLIENT_TEAM_ACCESS.ALL]

    def permissions(self, user, inherited=None):
        perms = super(Organization, self).permissions(user, inherited)
        if user and user in self.owners.users:
            perms.add('view')
            perms.add('edit')
            perms.add('delete')
            perms.add('view-teams')
            perms.add('new-team')
        else:
            if 'view' in perms:
                perms.remove('view')
            if 'edit' in perms:
                perms.remove('edit')
            if 'delete' in perms:
                perms.remove('delete')
        return perms

    @classmethod
    def get(cls, name=None, userid=None):
        """
        Return an Organization with matching name or userid. Note that ``name`` is the username, not the title.

        :param str name: Name of the organization
        :param str userid: Userid of the organization
        """
        if not bool(name) ^ bool(userid):
            raise TypeError("Either name or userid should be specified")

        if userid:
            return cls.query.filter_by(userid=userid).first()
        else:
            return cls.query.filter_by(name=name).first()

    def available_permissions(self):
        """
        Return all permission objects available to this organization
        (either owned by this organization or available to all users).
        """
        from .client import Permission
        return Permission.query.filter(
            db.or_(Permission.allusers == True, Permission.org == self)
            ).order_by(Permission.name).all()


class Team(BaseMixin, db.Model):
    __tablename__ = 'team'
    __bind_key__ = 'lastuser'
    #: Unique and non-changing id
    userid = db.Column(db.String(22), unique=True, nullable=False, default=newid)
    #: Displayed name
    title = db.Column(db.Unicode(250), nullable=False)
    #: Organization
    org_id = db.Column(db.Integer, db.ForeignKey('organization.id'), nullable=False)
    org = db.relationship(Organization, primaryjoin=org_id == Organization.id,
        backref=db.backref('teams', order_by=title, cascade='all, delete-orphan'))
    users = db.relationship(User, secondary='team_membership',
        backref='teams')  # No cascades here! Cascades will delete users

    def __repr__(self):
        return u'<Team {team} of {org}>'.format(team=self.title, org=self.org.title)

    @property
    def pickername(self):
        return self.title

    def permissions(self, user, inherited=None):
        perms = super(Team, self).permissions(user, inherited)
        if user and user in self.org.owners.users:
            perms.add('edit')
            perms.add('delete')
        return perms

    @classmethod
    def migrate_user(cls, olduser, newuser):
        for team in olduser.teams:
            if team not in newuser.teams:
                newuser.teams.append(team)
        olduser.teams = []
