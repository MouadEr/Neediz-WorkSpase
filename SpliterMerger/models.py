from google.appengine.ext import db
from google.appengine.ext import ndb

from oauth2client.appengine import CredentialsNDBProperty


class CredentialsModel(ndb.Model):
    credentials = CredentialsNDBProperty()

class StateModel(ndb.Model):
    state_key = ndb.KeyProperty()
    user_email = ndb.StringProperty()
    file_id = ndb.StringProperty()
    step = ndb.StringProperty()