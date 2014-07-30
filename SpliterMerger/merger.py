#!/usr/bin/python
# -*- coding: utf-8 -*-
# Add the library location to the path
import sys
sys.path.insert(0, 'lib')

import webapp2
import urllib
import urlfetch
import httplib2
import logging
import pyPdf
from oauth2client.client import flow_from_clientsecrets
from oauth2client.client import FlowExchangeError
from apiclient.discovery import build
from apiclient import errors

from apiclient.http import MediaFileUpload
from cStringIO import StringIO
from apiclient.http import MediaInMemoryUpload

from httplib import HTTPException

from google.appengine.api import urlfetch
from google.appengine.api import users
from oauth2client.appengine import StorageByKeyName
from models import CredentialsModel

urlfetch.set_default_fetch_deadline(60)
httplib2.Http(timeout=60)

from google.appengine.api import taskqueue
import time

from google.appengine.api import channel
from google.appengine.ext import deferred
import webapp2

from webapp2_extras import jinja2
import uuid

##
## Constant declaration
##

CLIENTSECRETS_LOCATION = 'client_secrets.json'
REDIRECT_URI = 'http://www.neediz-ws-splitpdf.appspot.com/oauth2callback'
SCOPES = [
    #'https://www.googleapis.com/auth/drive.file',
    'https://www.googleapis.com/auth/drive',
    'https://www.googleapis.com/auth/userinfo.email',
    'https://www.googleapis.com/auth/userinfo.profile',
    # Add other requested scopes.
]

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

    def retrieve_all_files(self, service):
      result = []
      page_token = None
      # while True > to get all files
      while len(result) < 10:
        try:
          param = {}
          if page_token:
            param['pageToken'] = page_token

          query = "mimeType = 'application/vnd.google-apps.document' or mimeType = 'application/vnd.google-apps.presentation' " \
                  "or mimeType = 'application/vnd.google-apps.spreadsheet' or mimeType = 'application/vnd.google-apps.drawing'	" \
                  "or mimeType = 'application/pdf' and trashed = false"
          files = service.files().list(q = query, maxResults=50).execute()

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

    def convert(self, meta_file, new_name, service, http):
        try:
            download_url = meta_file['exportLinks']['application/pdf']
            data = http.request(download_url, "GET")[1]
            return self.insert_file(service, new_name, data, 'application/pdf')
        except KeyError:
            self.response.write('Only google doc can be converted')
            return None


    ##
    ## Start treatment ##
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
            files_list = self.retrieve_all_files(service)
            self.response.write('<form method="post" action="rename">')
            self.response.write('<ol>')
            for meta_file in files_list:
                self.response.write('<input type="checkbox" name="files_id" value ="'
                                    + str(meta_file['id']) + '">' + meta_file['title'] + '</input><br/>')
            self.response.write(' <br/><br/><input type="submit" value="test">')
            self.response.write('</ol>')
            self.response.write('</form>')
        else:
            self.redirect('/')


    def rename(self):
        user = users.get_current_user()
        if user:
            credentials = self.get_stored_credentials(user.email())
            service = self.build_service(credentials)
            files_id = self.request.get_all('files_id')
            self.response.write('<form method="post" action="merge">')
            self.response.write('<h3>Saisir le nom du nouveau fichier</h3>')
            self.response.write('<input type="text" name="new_name"><br/><br/><br/>')
            self.response.write('<h3>Les fichier a concatener : </h3>')
            self.response.write('<ol>')
            for file_id in files_id:
                meta_file = self.get_meta_file(file_id, service)
                self.response.write('<li>' + meta_file['title'] + '</li>')
                self.response.write('<input type="hidden" name="files_id" value="' + meta_file['id'] + '">')
            self.response.write('</ol>')
            self.response.write('<br/><br/><input type="submit" value="merge">')
            self.response.write('</form>')
        else:
            self.redirect('/')


    def merge(self):
        user = users.get_current_user()
        if user:
            files_id = self.request.get_all('files_id')
            new_name = self.request.get('new_name')
            dict_param = {}
            i = 0
            for file_id in files_id:
                i += 1
                dict_param.update({str(i):file_id})
            dict_param.update({'number_file':str(i)})
            dict_param.update({'new_name':new_name})
            dict_param.update({'email':user.email()})

            client_id = user.user_id()
            channel_token = channel.create_channel(client_id)
            dict_param.update({'id':client_id})
            taskqueue.add(queue_name='supermerger', url='/supermerger', params=dict_param )

            self.render_response('home.html',**{"token":channel_token,"client_id":client_id})
        else:
            self.redirect('/')

    def supermerger(self):
        logging.info('Starting task queue')

        # Récupération des parametres
        email = self.request.get('email')
        new_name = self.request.get('new_name')
        number_file = self.request.get('number_file')
        files_id = []
        for p in range(1, int(number_file) + 1):
            files_id.append(self.request.get(str(p)))

        credentials = self.get_stored_credentials(email)
        service = self.build_service(credentials)
        writer = pyPdf.PdfFileWriter()
        http = self.get_http(credentials)

        avancement = 0
        for file_id in files_id:
            is_pdf = True
            meta_file = service.files().get(fileId=file_id).execute()

            avancement += 1
            channel.send_message(self.request.get('id'), 'merge en cours : ' + meta_file['title'] + '  ' + str(avancement) + '/' + str(len(files_id)))
            logging.info('merge en cours : ' + meta_file['title'] + '  ' + str(avancement) + '/' + str(len(files_id)))


            # Convert if not pdf
            if meta_file['mimeType'] != 'application/pdf':
                # Create a temp.pdf of this file
                download_url = meta_file['exportLinks']['application/pdf']
                data = http.request(download_url, "GET")[1]

                media_body = MediaInMemoryUpload(data, mimetype='text/plain', resumable=True)
                body = {
                  'title': 'temp',
                  'mimeType': 'application/pdf'
                }
                meta_file = service.files().insert(body=body, media_body=media_body).execute()
                is_pdf = False

            file_id = meta_file['id']
            # Download the pdf file.
            pdf_data = self.get_data_file(http, service, file_id)
            pdf = pyPdf.PdfFileReader(StringIO(pdf_data))
            num_page = pdf.getNumPages()

            pdfinput = pyPdf.PdfFileReader(StringIO(pdf_data))
            for i in xrange(num_page):
                writer.addPage(pdf.getPage(i))

            if not is_pdf:
                # Delete from drive the temp.pdf
                service.files().delete(fileId=file_id).execute()

        output_data = StringIO()
        writer.write(output_data)
        media_body = MediaInMemoryUpload(output_data.getvalue(), mimetype='text/plain', resumable=True)
        body = {
          'title': new_name + '.pdf',
          'mimeType': 'application/pdf'
        }
        service.files().insert(body=body, media_body=media_body).execute()

        # Success Message
        channel.send_message(self.request.get('id'), 'Fin du merge, Succés de l opération')
        logging.info("Succés de l'opération")


application = webapp2.WSGIApplication([
        webapp2.Route(r'/', handler=MainPage, name='', handler_method='get'),
        webapp2.Route(r'/oauth2callback', handler=MainPage, name='', handler_method='connexion'),
        webapp2.Route(r'/work', handler=MainPage, name='', handler_method='work'),
        webapp2.Route(r'/rename', handler=MainPage, name='', handler_method='rename'),
        webapp2.Route(r'/merge', handler=MainPage, name='', handler_method='merge'),
        webapp2.Route(r'/supermerger', handler=MainPage, name='', handler_method='supermerger')
                                      ], debug=True)