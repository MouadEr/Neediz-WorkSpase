#!/usr/bin/python
# -*- coding: utf-8 -*-
import sys
sys.path.insert(0, 'lib')

import webapp2
import urllib
import httplib2
import logging
import pyPdf
import time
import uuid

from cStringIO import StringIO
from webapp2_extras import jinja2

from apiclient import errors
from apiclient.discovery import build
from apiclient.http import MediaFileUpload
from apiclient.http import MediaInMemoryUpload

from google.appengine.api.urlfetch_errors import DeadlineExceededError

from google.appengine.api import urlfetch
from google.appengine.api import taskqueue
from google.appengine.api import users
from google.appengine.api import channel
from google.appengine.ext import deferred

from oauth2client.client import flow_from_clientsecrets
from oauth2client.client import FlowExchangeError
from oauth2client.appengine import StorageByKeyName

from models import CredentialsModel
from models import StateModel
from google.appengine.ext import ndb


httplib2.Http(timeout=60)
urlfetch.set_default_fetch_deadline(60)


##
## Constant declaration
##

CLIENTSECRETS_LOCATION = 'client_secrets.json'
REDIRECT_URI = 'http://www.buoyant-volt-630.appspot.com/oauth2callback'
SCOPES = [
    #'https://www.googleapis.com/auth/drive.file',
    'https://www.googleapis.com/auth/drive',
    'https://www.googleapis.com/auth/userinfo.email',
    'https://www.googleapis.com/auth/userinfo.profile',
    # Add other requested scopes.
]

## Gestion des portions en input

import re

def get_portion(suite):
    suite = suite.split(',')
    for i in range(len(suite)):
        suite[i] = suite[i].split('-')
        suite[i][0] = int(suite[i][0])
        suite[i][-1] = int(suite[i][-1])
    return suite

def regex(suite):
    unique = r"^(( *)\d+( *)|( *)\d+( *)-( *)\d+( *))(,(( *)\d+( *)|( *)\d+( *)-( *)\d+( *)))*$"
    a = re.compile(unique)
    if a.match(suite):
        return True
    return False

def limit_order(suite, num_page):
    suite = get_portion(suite)
    for portion in suite:
        if portion[-1] < portion[0]:
            return False
        for number in portion:
            if number - int(num_page) > 0:
                return False
    return True

def is_valid_exp(suite, num_page):
    if regex(suite):
        if limit_order(suite, num_page):
            return True
    return False




##
## Exception ##
##
class GetCredentialsException(Exception):
  """Error raised when an error occurred while retrieving credentials.

  Attributes:
    authorization_url: Authorization URL to redirect the user to in order to
                       request offline access.
  """
  def __init__(self, authorization_url):
    """Construct a GetCredentialsException."""
    self.authorization_url = authorization_url

class CodeExchangeException(GetCredentialsException):
  """Error raised when a code exchange has failed."""

class NoRefreshTokenException(GetCredentialsException):
  """Error raised when no refresh token has been found."""

class NoUserIdException(Exception):
  """Error raised when no user ID could be retrieved."""


class BaseHandler(webapp2.RequestHandler):
    @webapp2.cached_property
    def jinja2(self):
        # Returns a Jinja2 renderer cached in the app registry.
        j = jinja2.get_jinja2(app=self.app)
        #        j.environment.globals['is_admin']= is_admin()
        pass
        return j

    def render_response(self, _template, **context):
        # Renders a template and writes the result to the response.
        rv = self.jinja2.render_template(_template, **context)
        self.response.write(rv)


class MainPage(BaseHandler):

##
## Useful method ##
##
    def get_stored_credentials(self, user_email):
        credentials = StorageByKeyName(CredentialsModel, user_email,'credentials').locked_get()
        return credentials

    def store_credentials(self, user_email, credentials):
        StorageByKeyName(CredentialsModel, user_email, 'credentials').locked_put(credentials)

    def exchange_code(self, authorization_code):
      flow = flow_from_clientsecrets(CLIENTSECRETS_LOCATION, ' '.join(SCOPES))
      flow.redirect_uri = REDIRECT_URI
      try:
        credentials = flow.step2_exchange(authorization_code)
        return credentials
      except FlowExchangeError, error:
        logging.error('An error occurred: %s', error)
        raise CodeExchangeException(None)

    def get_user_info(self, credentials):
      """
      Args: credentials: oauth2client.client.OAuth2Credentials instance to authorize the request.
      Returns: User information as a dict.
      """
      user_info_service = build(serviceName='oauth2', version='v2', http=credentials.authorize(httplib2.Http()))
      user_info = None
      try:
        user_info = user_info_service.userinfo().get().execute()
      except errors.HttpError, e:
        logging.error('An error occurred: %s', e)
      if user_info and user_info.get('id'):
        return user_info
      else:
        raise NoUserIdException()

    def get_authorization_url(self, email_address, state):
      """Retrieve the authorization URL to redirect the user to. """
      flow = flow_from_clientsecrets(CLIENTSECRETS_LOCATION, ' '.join(SCOPES))
      flow.params['access_type'] = 'offline'
      flow.params['approval_prompt'] = 'force'
      flow.params['user_id'] = email_address
      flow.params['state'] = state
      return str(flow.step1_get_authorize_url(REDIRECT_URI))

    #Final
    def get_credentials(self, authorization_code, state):
      email_address = ''
      try:
        credentials = self.exchange_code(authorization_code)
        user_info = self.get_user_info(credentials)
        email_address = user_info.get('email')
        user_id = user_info.get('id')
        if credentials.refresh_token is not None:
          self.store_credentials(email_address, credentials)
          return credentials
        else:
          credentials = self.get_stored_credentials(email_address)
          if credentials and credentials.refresh_token is not None:
            return credentials
      except CodeExchangeException, error:
        logging.error('An error occurred during code exchange.')
        error.authorization_url = self.get_authorization_url(email_address, state)
        raise error
      except NoUserIdException:
        logging.error('No user ID could be retrieved.')
      # No refresh token has been retrieved.
      authorization_url = self.get_authorization_url(email_address, state)
      raise NoRefreshTokenException(authorization_url)

    def build_service(self, credentials):
      http = httplib2.Http()
      http = credentials.authorize(http)
      credentials.refresh(http)
      return build('drive', 'v2', http=http)

    def get_http(self, credentials):
      http = httplib2.Http()
      return credentials.authorize(http)


##
## Api Treatement ##
##

    def retrieve_all_files(self, service, query):
      result = []
      page_token = None
      # while True > to get all files
      while len(result) < 10:
        try:
          param = {}
          if page_token:
            param['pageToken'] = page_token
          files = service.files().list(q=query, maxResults=100).execute()

          result.extend(files['items'])
          page_token = files.get('nextPageToken')
          if not page_token:
            break
        except errors.HttpError, error:
          print 'An error occurred: %s' % error
          break
      return result

    def get_meta_file(self, file_id, service):
        try:
            file = service.files().get(fileId=file_id).execute()
            return file
        except errors.HttpError, error:
            logging.info('error occured')
            return None

    def get_data_file(self, http, service, file_id):
        url = service.files().get(fileId=file_id).execute()['downloadUrl']
        return http.request(url, "GET")[1]

    def insert_file(self, service, file_name, data, mimeType):
        media_body = MediaInMemoryUpload(data, mimetype='text/plain', resumable=True)
        body = {
          'title': file_name,
          'mimeType': mimeType
        }
        return service.files().insert(body=body, media_body=media_body).execute()

    def create_folder(self, service, folder_name, parentID):
        # Create a folder on Drive, returns the newely created folders ID
        body = {
          'title': folder_name,
          'mimeType': "application/vnd.google-apps.folder"
        }
        if parentID:
            body['parents'] = [{'id': parentID}]
        root_folder = service.files().insert(body = body).execute()
        return root_folder['id']

    def insert_file_into_folder(self, service, folder_id, file_id):
      new_parent = {'id': folder_id}
      try:
        return service.parents().insert(
            fileId=file_id, body=new_parent).execute()
      except errors.HttpError, error:
        print 'An error occurred: %s' % error
      return None

##
## START TREATMENT ##
##

    def get(self):
        # Run through the OAuth flow and retrieve credentials
      user = users.get_current_user()
      if user:
        credentials = self.get_stored_credentials(user.email())
        if credentials is None:
            self.redirect(self.get_authorization_url(user.email(), None))
        else:
            self.redirect('/work')
      else:
        self.redirect(users.create_login_url())


    def connexion(self):
        authorisation_code = self.request.get('code')
        credentials = self.get_credentials(authorisation_code, None)
        self.redirect('/work')


    def work(self):
        user = users.get_current_user()
        if user:
            credentials = self.get_stored_credentials(user.email())
            service = self.build_service(credentials)
            files_list = self.retrieve_all_files(service, "mimeType='application/pdf' and trashed = false")
            self.response.write('<ol>')
            for file in files_list:
                query_params = {'file_id': file['id']}
                lien = '/rename?' + urllib.urlencode(query_params)
                self.response.write('<li><a href="' + lien + '">' + file['title'] + '</a></li>')
            self.response.write('</ol>')
        else:
            self.redirect('/')


    def rename(self):
        user = users.get_current_user()
        if user:
            credentials = self.get_stored_credentials(user.email())
            service = self.build_service(credentials)

            file_id = self.request.get('file_id')
            meta_file = self.get_meta_file(file_id, service)
            file_name = meta_file['title']
            folders_list = self.retrieve_all_files(service, "mimeType='application/vnd.google-apps.folder' and trashed = false")

            # Download the pdf file.
            http = self.get_http(credentials)
            pdf_data = self.get_data_file(http, service, file_id)
            pdf = pyPdf.PdfFileReader(StringIO(pdf_data))
            num_page = pdf.getNumPages()


            self.response.write( self.request.get('expression') + ' <hr/> ')
            self.response.write('<h1> Nombre de page du pdf  ' + file_name + ' : ' + str(num_page) + '</h1>')

            self.response.write('<br/><br/><br/><form method="post" action="split">')
            self.response.write('<input type="hidden" name="file_id" value="' + str(file_id) + '">')
            self.response.write('<h3> Saisir le nom du dossier</h3>')
            self.response.write(' Nom du dossier : <input type="text" name="folder_name"><br/>'
            '<i>( Si aucun nom de dossier n est saisi, les pdf seront stockés directement dans le dossier choisis ci dessous )</i><br/><br/><br/>')
            self.response.write('<h5> Choisir dossier de destination</h5>')
            for folder in folders_list:
                self.response.write('<input type="radio" name="folder_id" value="' + folder['id'].encode('utf-8') + '">' + folder['title'].encode('utf-8') + '<br/>')
            self.response.write('<i>( Si aucun dossier choisi, stockage dans la racine )</i><br/><br/><br/>')

            self.response.write(' Nom globale : <input type="text" name="nom_globale">')
            self.response.write('<h3> Saisir le style de découpage </h3>')
            self.response.write('<input type="text" name="suite" placeholder="exemple 1-5, 7, 8-10 ">')
            self.response.write(' <br/><br/><input type="hidden" name="num_pages" value="' + str(num_page) + '">')
            self.response.write(' <br/><br/><input type="submit" value="Split">')
            self.response.write('</form>')
        else:
            self.redirect('/')


    def split(self):
        user = users.get_current_user()
        if user:
            file_id = self.request.get('file_id')
            folder_id = self.request.get('folder_id')
            folder_name = self.request.get('folder_name')
            nom_globale = self.request.get('nom_globale')
            num_pages = self.request.get('num_pages')
            suite = self.request.get('suite')

            if not is_valid_exp(suite, num_pages):
                query_params = {'file_id': file_id, 'expression': ' Expression Invalide '}
                self.redirect('/rename?' + urllib.urlencode(query_params))

            else:
                dict_param = {}
                dict_param.update({'email':user.email()})
                dict_param.update({'file_id':file_id})
                dict_param.update({'folder_id':folder_id})
                dict_param.update({'folder_name':folder_name})
                dict_param.update({'nom_globale':nom_globale})
                dict_param.update({'suite':suite})


                client_id = user.user_id()
                channel_token = channel.create_channel(client_id)
                dict_param.update({'id':client_id})
                taskqueue.add(queue_name='spliterportion', url='/superspliterportion', params=dict_param )

                self.render_response('home.html',**{"token":channel_token,"client_id":client_id})

        else:
            self.redirect('/')


    def superspliterportion(self):
        state = StateModel()

        is_folder_destination = False
        logging.debug('Start spliting')
        file_id = self.request.get('file_id')
        folder_id = self.request.get('folder_id')
        if folder_id:
            is_folder_destination = True
        email = self.request.get('email')
        suite = self.request.get('suite')

        credentials = self.get_stored_credentials(email)
        service = self.build_service(credentials)
        meta_file = self.get_meta_file(file_id, service)
        file_name = meta_file['title']

        # Create State that gonna be used if Error
        state = StateModel.query(ndb.AND(StateModel.user_email == email, StateModel.file_id == file_id)).get()
        state_stored = True
        if not state:
            state_stored = False
            state = StateModel()
            state.user_email = email
            state.file_id = file_id
            state.step = '0'

        # Create folder if none
        folder_name = self.request.get('folder_name')
        if state.step == '0':
            # Create folder (Step 1)
            logging.debug('STEP 0')

            if folder_name != '':
                is_folder_destination = True
                body = {
                  'title': folder_name,
                  'mimeType': "application/vnd.google-apps.folder"
                }
                if folder_id:
                    body['parents'] = [{'id': folder_id}]
                root_folder = service.files().insert(body = body).execute()
                folder_id = root_folder['id']

            state.step = '1'

        try:
            # Download the pdf file.
            http = self.get_http(credentials)
            pdf_data = self.get_data_file(http, service, file_id)
            pdf = pyPdf.PdfFileReader(StringIO(pdf_data))
            num_page = pdf.getNumPages()

            # Get Names
            suite = get_portion(suite)
            nom_globale = self.request.get('nom_globale')
            nom_portion = []
            for portion in suite:
                nom_portion.append(nom_globale + str(portion))


            # Create new pdf for each page (Step 2 > num_portion + 2)
            num_portion = 0
            for p in suite:
                logging.debug('Num Portion = ' + str(num_portion) + ', Actuel Step = ' + str(int(state.step) - 2))
                if num_portion > int(state.step) - 2:
                    writer = pyPdf.PdfFileWriter()
                    for num_page_portion in xrange(p[0] - 1, p[-1]):
                        writer.addPage(pdf.getPage(num_page_portion))

                    portion_data = StringIO()
                    writer.write(portion_data)
                    media_body = MediaInMemoryUpload(portion_data.getvalue(), mimetype='text/plain', resumable=True)
                    body = {
                      'title': nom_portion[num_portion] + '.pdf',
                      'mimeType': 'application/pdf'
                    }
                    portion = service.files().insert(body=body, media_body=media_body).execute()
                    try:
                        if is_folder_destination:
                            root_folder_id = service.about().get().execute()["rootFolderId"]
                            service.parents().delete(fileId=portion['id'], parentId=root_folder_id).execute()
                        service.parents().insert(fileId=portion['id'], body={'id': folder_id}).execute()
                    except errors.HttpError, error:
                        logging.info( 'An error occurred: %s' % error )

                    #
                    state.step = str(num_portion + 2)
                    logging.debug('Doing Job > split en cours : ' + str(num_portion + 1) + '/' + str(len(suite)))
                    channel.send_message(self.request.get('id'), 'split en cours : portion ' + str(num_portion + 1) + '/' + str(len(suite)))

                    # Soulévement d'erreur
                    #logging.warning(str(int(round(time.time() * 1000))))
                    #if int(round(time.time() * 1000)) % 2 == 0:
                    #    raise MyError('Erreur of Time')

                num_portion += 1

            # Fin > Succés > Supprimer state
            if state_stored:
                logging.debug('A State was Stored =), STEP =' + str(state.step))
                state_key = StateModel.query(ndb.AND(StateModel.user_email == email, StateModel.file_id == file_id)).get(keys_only=True)
                state.state_key = state_key
                state.state_key.delete()

            channel.send_message(self.request.get('id'), 'Fin du split')
            logging.debug('Fin du split')

        #except DeadlineExceededError as e:
        except Exception as e:
            logging.warning(e)
            state.put()
            logging.debug('Storing a State >> , STEP =' + str(state.step))
            channel.send_message(self.request.get('id'), 'Simulation d erreur, Continue ...')
            raise MyError('Recommencer a partir de l ancien')



application = webapp2.WSGIApplication([
        webapp2.Route(r'/', handler=MainPage, name='', handler_method='get'),
        webapp2.Route(r'/oauth2callback', handler=MainPage, name='', handler_method='connexion'),
        webapp2.Route(r'/work', handler=MainPage, name='', handler_method='work'),
        webapp2.Route(r'/rename', handler=MainPage, name='', handler_method='rename'),
        webapp2.Route(r'/split', handler=MainPage, name='', handler_method='split'),
        webapp2.Route(r'/superspliterportion', handler=MainPage, name='', handler_method='superspliterportion')
                                      ], debug=True)