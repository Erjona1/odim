import asyncio
import inspect
import os
import re
import threading
import urllib
from typing import Optional
import pydantic

settings_module = None
modsloaded = False

def get_config(confvar, default=None):
  global settings_module, modsloaded
  o = os.environ.get(confvar)
  env = os.path.isfile('.env')
  if env:
    return {
      'mysqldb': {
      'host': os.getenv('DATABASE_HOST'),
      'db': os.getenv('DATABASE_NAME'),
      'port': os.getenv('DATABASE_PORT'),
      'username': os.getenv('DATABASE_USER'),
      'password': os.getenv('DATABASE_PASSWORD'),
      'protocol': 'mysqldb'
      }
      }
  if o:
    return o
  if not modsloaded:
    try:
      settings_module = __import__('settings')
    except ModuleNotFoundError:
      settings_module = __import__('config')
    modsloaded = True
  if settings_module:
    if hasattr(settings_module,"get"):
      return settings_module.get(confvar, default)
    return getattr(settings_module, confvar, default)
  return default


connectors = None
odim_module = None

def get_base_from_module(module, parent_class):
  for n,x in inspect.getmembers(module, inspect.isclass):
    if issubclass(x, parent_class) and x!=parent_class and module.__name__ == x.__module__:
      return x


def get_connector_for_model(model):
  global connectors, odim_module
  if not connectors:
    connectors = [
      __import__('odim.mongo', fromlist=['odim']),
      __import__('odim.mysql', fromlist=['odim'])
    ]
    odim_module = __import__('odim')
  for connector in connectors:
    basemod = get_base_from_module(connector, pydantic.main.BaseModel)
    cls = model if inspect.isclass(model) else model.__class__
    if issubclass(cls, basemod):
      return get_base_from_module(connector, odim_module.Odim)

  if hasattr(model,'Config'):
    if hasattr(model.Config, 'db_name'):
      conn = get_connection_info(model.Config.db_name)
      if not conn:
        conn = get_connection_info(model.Config.db_uri)
      if conn:
        for connector in connectors:
          odim_class = get_base_from_module(connector, odim_module.Odim)
          if conn.protocol in odim_class.protocols:
            return odim_class

  raise AttributeError("No connector was found for instance class. Do you have the db_name or db_uri Config attr set?")



class ConnParams(pydantic.BaseModel):
  protocol : str
  host : str
  port : Optional[int] = None
  username : Optional[str] = None
  password : Optional[str] = None
  db : Optional[str]

  def url(self, withdb=True):
    u = self.protocol+"://"
    if self.username:
      u+= self.username+":"+self.password
    u+= self.host
    if self.port:
      u+= ":"+str(self.port)
    if withdb and self.db:
      u+= "/"+self.db
    return u


def get_connection_info(db) -> ConnParams:
  dbs = get_config('DATABASES')
  if db in dbs:
    if not isinstance(dbs[db], str):
      return ConnParams(**dbs[db])
    else:
      db = dbs[db]
  parsed = urllib.parse.urlparse(db)
  cp = ConnParams(protocol=parsed.scheme, host=parsed.hostname)
  if parsed.port:
    cp.port = parsed.port
  if parsed.username:
    cp.username = urllib.parse.unquote(parsed.username)
    cp.password = urllib.parse.unquote(parsed.password)
  if parsed.path:
    cp.db = parsed.path[1:]
  return cp

class RunThread(threading.Thread):
  def __init__(self, func):
    self.func = func
    super().__init__()

  def run(self):
    try:
      loop = asyncio.get_event_loop() or asyncio.new_event_loop()
      asyncio.set_event_loop(loop)
    except RuntimeError as e:
      loop = None
    if inspect.iscoroutine(self.func):

      if loop and loop.is_running():
        self.result = loop.run_until_complete(self.func)
      elif loop and not loop.is_running():
        self.result = asyncio.run(asyncio.ensure_future(self.func))
      else:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        self.result = loop.run_until_complete(self.func)
        # self.result = asyncio.run(asyncio.ensure_future(self.func))
    else:
      self.result = self.func
    
      
def awaited(func):
  if inspect.isfunction(func) or inspect.iscoroutine(func):
    try:
      thread = RunThread(func)
      thread.start()
      thread.join()
      try:
        return thread.result
      except AttributeError:
        return None
    except AttributeError as e:
      return None
  else:
    return func


def camel_case_to_snake_case(name):
  s1 = re.sub('(.)([A-Z][a-z]+)', r'\1_\2', name)
  return re.sub('([a-z0-9])([A-Z])', r'\1_\2', s1).lower()

def snake_case_to_camel_case(value):
  if "_" in value:
    return "".join(ele.title() for ele in value.split("_"))
  else:
    return value
  
