import logging
import re
from enum import Enum
from typing import List, Optional, Union

import aiomysql.cursors
from pydantic import BaseModel, Field
from pymysql.converters import escape_bytes_prefixed, escape_item, escape_string
from sqlalchemy.sql import text
from sqlalchemy import and_, or_, not_


from odim import BaseOdimModel, NotFoundException, Odim, Operation, SearchParams, get_connection_info

log = logging.getLogger("uvicorn")
pools = {}


async def connected_pool(db):
  global pools
  if not db in pools:
    cn = get_connection_info(db)
    if not cn.port:
      cn.port = 3306
    pools[db] =  await aiomysql.create_pool(host=cn.host, port=int(cn.port), user=cn.username, password=cn.password,
                                            db=cn.db, cursorclass=aiomysql.cursors.DictCursor)
  return pools[db]


class Op(Enum):
  execute = 0
  fetchone = 1
  fetchall = 2


async def execute_sql(db, sql, co : Op = Op.execute):
  log.info(sql)
  pool = await connected_pool(db)
  async with pool.acquire() as conn:
    cursor = await conn.cursor()
    executed = await cursor.execute(sql)
    if co == Op.fetchone:
      return await cursor.fetchone()
    elif co == Op.fetchall:
      return await cursor.fetchall()
    else: #execute
      await conn.commit()
      return cursor

    #TODO handle disconnects and ends
    #TODO pymysql.err.OperationalError



class BaseMysqlModel(BaseOdimModel):
  id: Optional[str] = Field(alias='id', description="Unique identifier of the record")


class OdimMysql(Odim):
  protocols = ["mysql", "mysqldb"]

  def escape(self, obj):
    """ Escape whatever value you pass to it"""
    if isinstance(obj, str):
      return "'" + escape_string(obj) + "'"
    if isinstance(obj, bytes):
      return escape_bytes_prefixed(obj)
    return escape_item(obj, getattr(self.model.Config, 'charset', 'utf-8'))


  def get_table_name(self):
    ci = self.get_connection_identifier
    if hasattr(self.model, 'Config'):
      if hasattr(self.model.Config, 'table_name'):
        cn = self.model.Config.table_name
        return ci, cn
      elif hasattr(self.model.Config, 'collection_name'):
        cn = self.model.Config.collection_name
        return ci, cn
    return ci, self.model.__class__.__name__


  async def get(self, id : str, extend_query : dict= {}, include_deleted : bool = False, field: str = ''):
    '''
    Retrieves the document by its id
    :param id: id of the docuemnt
    :param kwargs:
    :return: the document as pydantic instance '''
    #TODO just the desired fields
    db, table = self.get_table_name()
    if not id:
      query = {**extend_query}
    else:
      query = {"id" : id, **extend_query}
    if self.softdelete() and not include_deleted:
      query[self.softdelete()] = False
    wh = self.get_where(query)
    rsp = await execute_sql(db, "SELECT * FROM %s WHERE %s" % (escape_string(table), wh), Op.fetchone)
    if not rsp:
      raise NotFoundException()
    ret = self.execute_hooks("pre_init", rsp)
    x = self.model(**ret)
    obj = self.execute_hooks("post_init", x)
    return getattr(obj, field) if field else obj

  def get_field_pairs(self, field_dict):
    inss = []
    for k, v in field_dict.items():
      if not re.match("[a-zA-Z0-9_]+", k):
        raise AttributeError("Writing a non ASCII field name")
      if k!="id" or not self.id_auto_inc():
        inss.append( "`"+k+"`="+str(self.escape(v)) )
    return ",".join(inss)

  async def save(self, extend_query : dict= {}, include_deleted : bool = False):
    ''' Saves the document and returns its identifier '''
    db, table = self.get_table_name()
    new_record = not hasattr(self.instance, 'id') or self.instance.id in (None, "")
    iii = self.execute_hooks("pre_save", self.instance, created=new_record)
    do = iii.dict(by_alias=True)

    if new_record:
      if self.softdelete() and self.softdelete() not in do:
        do[self.softdelete()] = False
      upff = self.get_field_pairs({**extend_query, **do})
      rsp = await execute_sql(db, "INSERT INTO %s SET %s" % (escape_string(table), upff), Op.execute)
      # setattr(self.instance, 'id', rsp.lastrowid)
      # iii.id = self.instance.id
      iii = self.execute_hooks("post_save", iii, created=True)
      return do['id']
      return rsp.lastrowid
    else:
      softdel = {self.softdelete(): False} if self.softdelete() and not include_deleted else {}
      upff = self.get_field_pairs(do)
      whr = self.get_where({"id" : self.instance.id, **softdel, **extend_query})
      sql = "UPDATE %s SET %s WHERE %s" % (escape_string(table), upff, whr)
      rsp = await execute_sql(db, sql, Op.execute)
      iii = self.execute_hooks("post_save", iii, created=False)
      return self.instance.id


  async def update(self, extend_query : dict= {}, include_deleted : bool = False, only_fields : Optional[List['str']] = None):
    ''' Updates just the partial document '''
    db, table = self.get_table_name()
    iii = self.execute_hooks("pre_save", self.instance, created=False)
    dd = iii.dict(exclude_unset=True, by_alias=True)
    dd_id = dd["id"]
    del dd["id"]
    if only_fields and len(only_fields)>0:
      dd = dict([(key, val) for key, val in dd.items() if key in only_fields])
    softdel = {self.softdelete(): False} if self.softdelete() and not include_deleted else {}
    upff = self.get_field_pairs(dd)
    whr = self.get_where({"id" :dd_id, **softdel, **extend_query})
    sql = "UPDATE %s SET %s WHERE %s" % (escape_string(table), upff, whr)
    rsp = await execute_sql(db, sql, Op.execute)
    iii = self.execute_hooks("post_save", iii, created=False)


  def get_where(self, query):
    whr = []
    for k, (op, v) in self.parse_query_operations(query).items():
      k = k.replace('$', '')
      if not re.match("[a-zA-Z0-9_]+", k):
        raise AttributeError("Searching on a non ASCII field name")
      if op == Operation.exact:
        whr.append( "`"+k+"`="+str(self.escape(v)) )
      elif op == Operation.isnot:
        whr.append( "`"+k+"`!="+str(self.escape(v)) )
      elif op == Operation.contains:
        whr.append( "`"+k+"` LIKE '%"+str(escape_string(v)+"%'") )
      elif op == Operation.gt:
        whr.append( "`"+k+"` > "+str(self.escape(v)) )
      elif op == Operation.gte:
        whr.append( "`"+k+"` >= "+str(self.escape(v)) )
      elif op == Operation.lt:
        whr.append( "`"+k+"` < "+str(self.escape(v)) )
      elif op == Operation.lte:
        whr.append( "`"+k+"` <= "+str(self.escape(v)) )
      elif op == Operation.null:
        if v:
          whr.append( "`"+k+"` IS NULL" )
        else:
          whr.append( "`"+k+"` IS NOT NULL" )
    return  "1" if len(whr) == 0  else " AND ".join(whr)
  

  def dict_to_mysql_query(self, query):
    mysql_query = ""
    conditions = []
    for key, value in query.items():
      if key == "$or" or key == "$and":
          sub_conditions = []
          for sub_dict in value:
              sub_query = self.dict_to_mysql_query(sub_dict)
              sub_conditions.append(sub_query)
          if key == '$or':
            key = 'OR'
          elif key == '$and':
            key = 'AND'
          result = "(" + f" {key} ".join(str(x) for x in sub_conditions) + ")"
          conditions.append(result)
      elif isinstance(value, dict):
          sub_query = self.dict_to_mysql_query(value)
          conditions.append(key + " " + sub_query)
      elif key == "$not":
        conditions.append(" != " + str(value))
      elif key == "$regex":
          conditions.append(" LIKE '%" + value + "%'")
      elif key == "$lt":
          conditions.append(" < " + str(value))
      elif key == "$lte":
          conditions.append(" <= " + str(value))
      elif key == "$gt":
          conditions.append(" > " + str(value))
      elif key == "$gte":
          conditions.append(" >= " + str(value))
      elif key == "$in":
          conditions.append(" in " + str(tuple(value.split(','))))
      elif key == "$nin":
        conditions.append(" not in " + str(tuple(value.split(','))))
      elif key == "$lte":
          conditions.append(" <= " + str(value))
      elif key == "$nor":
        conditions.append("not (" + f" or ".join(str(self.dict_to_mysql_query(x)) for x in value) + ")")
      elif key == "$eq":
        conditions.append(key + " = '" + value + "'")
      elif key == "$ne":
        conditions.append(key + " != '" + value + "'")
      else:
          conditions.append(key + " = '" + value + "'")
    mysql_query += " AND ".join(conditions)
    return mysql_query

  async def find(self, query : dict, params : SearchParams = None, include_deleted : bool = False):
    ''' Performs search using a dictionary qury to find documents on that particular collection/table
    :param query: dictionary of field:value pairs
    :param params: additional search params like ordering and limit offset
    :return: the list of documents as per pydantic type    '''
    db, table = self.get_table_name()
    if self.softdelete() and not include_deleted:
      query = {self.softdelete(): False, **query}
    # where = self.get_where(query)
    sql_params = ""
    if params:
      if params.sort not in (None, ''):
        sql_params+= " ORDER BY "
        paramslist = []
        for x in params.sort.split(","):
          paramslist.append( (x[1:]+" DESC ") if x[0] == "-" else (x+" ASC ") )
        sql_params+= ",".join(paramslist)
      if params.limit:
        sql_params+= " LIMIT "+str(params.limit)
      if params.offset:
        sql_params+= " OFFSET "+str(params.offset)
    res = self.dict_to_mysql_query(query) or "1"
    rsp = await execute_sql(db, "SELECT * FROM %s WHERE %s %s" % (escape_string(table), res, sql_params), Op.fetchall)
    rsplist = []
    for row in rsp:
      x2 = self.execute_hooks("pre_init", row)
      m = self.model( **row )
      rsplist.append( self.execute_hooks("post_init", m) )
    return rsplist
  
  async def get_related(self, query, related):
    db, table = self.get_table_name()
    res = self.dict_to_mysql_query(query) or "1"
    q = "SELECT * FROM %s JOIN %s WHERE %s " %(escape_string(table), related.Config.collection_name, res)
    rsp = await execute_sql(db, q, Op.fetchone)
    ret = self.execute_hooks("pre_init", rsp)
    x = self.model(**ret)
    return self.execute_hooks("post_init", x)

  async def count(self, query : dict, include_deleted : bool = False) -> int:
    ''' Do the search and count the documents
    :param query: dictionary of field:value pairs
    :return: the number of results '''
    db, table = self.get_table_name()
    if self.softdelete() and not include_deleted:
      query = {self.softdelete(): False, **query}
    # where = self.get_where(query)
    res = self.dict_to_mysql_query(query) or "1"
    rsp = await execute_sql(db, "SELECT COUNT(*) as cnt FROM %s WHERE %s" % (escape_string(table), res), Op.fetchone)
    return rsp["cnt"]


  async def delete(self, obj : Union[str, int, BaseModel], extend_query : dict= {}, force_harddelete : bool = False):
    ''' Delete the document from storage '''
    db, table = self.get_table_name()
    id = obj if not isinstance(obj, BaseModel) else obj.id
    softdelete = self.softdelete() and not force_harddelete
    if self.has_hooks("pre_remove","post_remove"):
      x = await self.get(id)
      x = self.execute_hooks("pre_remove", x, softdelete=softdelete)
    if softdelete:
      whr = self.get_where({"id" : id, **extend_query})
      await execute_sql(db, "UPDATE %s SET `%s`=true WHERE %s" % (escape_string(table), self.softdelete(), whr), Op.execute)
    else:
      whr = self.get_where({"id" : id, **extend_query})
      await execute_sql(db, "DELETE FROM %s WHERE %s" % (escape_string(table), whr), Op.execute)
    if self.has_hooks("post_remove"):
      self.execute_hooks("post_remove", x, softdelete=softdelete)
    #TODO detect not found


