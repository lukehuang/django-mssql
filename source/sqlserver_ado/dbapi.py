"""adodbapi v2.1D -  A (mostly) Python DB API 2.0 interface to Microsoft ADO

Python's DB-API 2.0: http://www.python.org/dev/peps/pep-0249/

Copyright (C) 2002  Henrik Ekelund

This library is free software; you can redistribute it and/or
modify it under the terms of the GNU Lesser General Public
License as published by the Free Software Foundation; either
version 2.1 of the License, or (at your option) any later version.

This library is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
Lesser General Public License for more details.

You should have received a copy of the GNU Lesser General Public
License along with this library; if not, write to the Free Software
Foundation, Inc., 59 Temple Place, Suite 330, Boston, MA  02111-1307  USA

Version 2.1 by Vernon Cole
Version 2.1D by Adam Vandenberg (forked for internal Django backend use)
"""

import sys
import time
import datetime
import re

try:
    import decimal
except ImportError: # for Python 2.3
    from django.utils import _decimal as decimal

from win32com.client import Dispatch

import pythoncom
pythoncom.__future_currency__ = True # Request Python decimal from COM layer

from ado_consts import *
from util import MultiMap

apilevel = '2.0' # String constant stating the supported DB API level.

# Level of thread safety this interface supports:
# 1: Threads may share the module, but not connections.
threadsafety = 1

# The underlying ADO library expects parameters as '?', but this wrapper 
# expects '%s' parameters. This wrapper takes care of the conversion.
paramstyle = 'format'

version = __doc__.split('-',2)[0]


#  Set defaultIsolationLevel on module level before creating the connection.
#   It may be one of "adXact..." consts.
defaultIsolationLevel = adXactReadCommitted

#  Set defaultCursorLocation on module level before creating the connection.
#   It may be one of the "adUse..." consts.
defaultCursorLocation = adUseServer

# Used for COM to Python date conversions.
_ordinal_1899_12_31 = datetime.date(1899,12,31).toordinal()-1
_milliseconds_per_day = 24*60*60*1000

# Used for munging string date times until #7560 lands:
# http://code.djangoproject.com/ticket/7560
rx_datetime = re.compile(r'^(\d{4}-\d\d?-\d\d? \d\d?:\d\d?:\d\d?.\d{3})\d{3}$')


def standardErrorHandler(connection, cursor, errorclass, errorvalue):
    err = (errorclass, errorvalue)
    connection.messages.append(err)
    if cursor is not None:
        cursor.messages.append(err)
    raise errorclass(errorvalue)


class Error(StandardError): pass
class Warning(StandardError): pass

class InterfaceError(Error):
    def __init__(self, inner_exception=None):
        self.inner_exception = inner_exception

    def __str__(self):
        s = "InterfaceError"
        if self.inner_exception is not None:
            s += "\n" + str(self.inner_exception)
        return s

class DatabaseError(Error): pass
class InternalError(DatabaseError): pass
class OperationalError(DatabaseError): pass
class ProgrammingError(DatabaseError): pass
class IntegrityError(DatabaseError): pass
class DataError(DatabaseError): pass
class NotSupportedError(DatabaseError): pass

class _DbType(object):
    def __init__(self,valuesTuple):
        self.values = valuesTuple

    def __eq__(self, other): return other in self.values
    def __ne__(self, other): return other not in self.values


def connect(connection_string, timeout=30):
    """Connect to a database.
    
    connection_string -- An ADODB formatted connection string, see:
        http://www.connectionstrings.com/?carrier=sqlserver2005
    timeout -- A command timeout value, in seconds (default 30 seconds)
    """
    pythoncom.CoInitialize()
    c = Dispatch('ADODB.Connection')
    c.CommandTimeout = timeout
    c.ConnectionString = connection_string

    try:
        c.Open()
    except Exception, e:
        print "Error attempting connection: " + connection_string
        raise DatabaseError(e)
        
    useTransactions = _use_transactions(c)
    return Connection(c, useTransactions)

def _use_transactions(c):
    """Return True if the given ADODB.Connection supports transations."""
    for prop in c.Properties:
        if prop.Name == 'Transaction DDL':
            return prop.Value > 0
    return False

def format_parameters(parameters):
    """Format a collection of ADO Command Parameters.
    
    Used by error reporting in _executeHelper.
    """
    desc = ["Name: %s, Type: %s, Size: %s" %\
        (p.Name, adTypeNames.get(p.Type, str(p.Type)+' (unknown type)'), p.Size)
        for p in parameters]
    
    return '[' + ', '.join(desc) + ']'


class Connection(object):
    def __init__(self, adoConn, useTransactions=False):
        self.adoConn = adoConn
        self.errorhandler = None
        self.messages = []
        self.adoConn.CursorLocation = defaultCursorLocation
        self.supportsTransactions = useTransactions
        
        if self.supportsTransactions:
            self.adoConn.IsolationLevel = defaultIsolationLevel
            self.adoConn.BeginTrans() #Disables autocommit per DBPAI

    def _raiseConnectionError(self, errorclass, errorvalue):
        eh = self.errorhandler
        if eh is None:
            eh = standardErrorHandler
        eh(self, None, errorclass, errorvalue)

    def _closeAdoConnection(self):
        """Close the underlying ADO Connection object, rolling back an active transation if supported."""
        if self.supportsTransactions:
            self.adoConn.RollbackTrans()
        self.adoConn.Close()

    def close(self):
        """Close the database connection."""
        self.messages = []
        try:
            self._closeAdoConnection()
        except Exception, e:
            self._raiseConnectionError(InternalError, e)
        pythoncom.CoUninitialize()

    def commit(self):
        """Commit a pending transaction to the database.

        Note that if the database supports an auto-commit feature, this must 
        be initially off.
        """
        self.messages = []
        if not self.supportsTransactions:
            return
            
        try:
            self.adoConn.CommitTrans()
            if not(self.adoConn.Attributes & adXactCommitRetaining):
                #If attributes has adXactCommitRetaining it performs retaining commits that is,
                #calling CommitTrans automatically starts a new transaction. Not all providers support this.
                #If not, we will have to start a new transaction by this command:
                self.adoConn.BeginTrans()
        except Exception, e:
            self._raiseConnectionError(Error, e)

    def rollback(self):
        """Abort a pending transation."""
        self.messages = []
        if not self.supportsTransactions:
            self._raiseConnectionError(NotSupportedError, None)
            
        self.adoConn.RollbackTrans()
        if not(self.adoConn.Attributes & adXactAbortRetaining):
            #If attributes has adXactAbortRetaining it performs retaining aborts that is,
            #calling RollbackTrans automatically starts a new transaction. Not all providers support this.
            #If not, we will have to start a new transaction by this command:
            self.adoConn.BeginTrans()

    def cursor(self):
        """Return a new Cursor object using the current connection."""
        self.messages = []
        return Cursor(self)

    def printADOerrors(self):
        print 'ADO Errors (%i):' % self.adoConn.Errors.Count
        for e in self.adoConn.Errors:
            print 'Description: %s' % e.Description
            print 'Error: %s %s ' % (e.Number, adoErrors.get(e.Number, "unknown"))
            if e.Number == ado_error_TIMEOUT:
                print 'Timeout Error: Try using adodbpi.connect(constr,timeout=Nseconds)'
            print 'Source: %s' % e.Source
            print 'NativeError: %s' % e.NativeError
            print 'SQL State: %s' % e.SQLState

    def __del__(self):
        try:
            self._closeAdoConnection()
        except: pass
        self.adoConn = None


class Cursor(object):
##    This read-only attribute is a sequence of 7-item sequences.
##    Each of these sequences contains information describing one result column:
##        (name, type_code, display_size, internal_size, precision, scale, null_ok).
##    This attribute will be None for operations that do not return rows or if the
##    cursor has not had an operation invoked via the executeXXX() method yet.
##    The type_code can be interpreted by comparing it to the Type Objects specified in the section below.
    description = None

##    This read-only attribute specifies the number of rows that the last executeXXX() produced
##    (for DQL statements like select) or affected (for DML statements like update or insert).
##    The attribute is -1 in case no executeXXX() has been performed on the cursor or
##    the rowcount of the last operation is not determinable by the interface.[7]
##    NOTE: -- adodbapi returns "-1" by default for all select statements
    rowcount = -1

    # Arraysize specifies the number of rows to fetch at a time with fetchmany().
    arraysize = 1

    def __init__(self, connection):
        self.messages = []
        self.connection = connection
        self.rs = None
        self.description = None
        self.errorhandler = connection.errorhandler

    def __iter__(self):
        return iter(self.fetchone, None)

    def _raiseCursorError(self, errorclass, errorvalue):
        eh = self.errorhandler
        if eh is None:
            eh = standardErrorHandler
        eh(self.connection, self, errorclass, errorvalue)

    def callproc(self, procname, parameters=None):
        """Call a stored database procedure with the given name."""
        self.messages = []
        return self._executeHelper(procname, True, parameters)

    def _returnADOCommandParameters(self, cmd):
        values = list()
        for p in cmd.Parameters:
            python_obj = _convert_to_python(p.Value, p.Type)
            if p.Direction == adParamReturnValue:
                self.returnValue = python_obj
            else:
                values.append(python_obj)

        return values

    def _description_from_recordset(self, recordset):
    	# Abort if closed or no recordset.
        if (recordset is None) or (recordset.State == adStateClosed):
            self.rs = None
            self.description = None
            return

        # Since we use a forward-only cursor, rowcount will always return -1
        self.rowcount = -1
        self.rs = recordset
        desc = list()
        
        for f in self.rs.Fields:            
            display_size = None            
            if not(self.rs.EOF or self.rs.BOF):
                display_size = f.ActualSize
                
            null_ok = bool(f.Attributes & adFldMayBeNull)
            
            desc.append( (f.Name, f.Type, display_size, f.DefinedSize, f.Precision, f.NumericScale, null_ok) )
        self.description = desc

    def close(self):
        """Close the cursor (but not the associated connection.)"""
        self.messages = []
        self.connection = None
        if self.rs and self.rs.State != adStateClosed:
            self.rs.Close()
            self.rs = None

    def _new_command(self, sproc_call):
        self.cmd = Dispatch("ADODB.Command")
        self.cmd.ActiveConnection = self.connection.adoConn
        self.cmd.CommandTimeout = self.connection.adoConn.CommandTimeout

        if sproc_call:
            self.cmd.CommandType = adCmdStoredProc
        else:
            self.cmd.CommandType = adCmdText

    def _configure_parameter(self, p, value):
        """Configure the given ADO Parameter 'p' with the Python 'value'."""
        if p.Direction not in [adParamInput, adParamInputOutput, adParamUnknown]:
            return
        
        if isinstance(value, basestring):
            p.Value = value
            p.Size = len(value)
        
        elif isinstance(value, buffer):
            p.Size = len(value)
            p.AppendChunk(value)

        elif isinstance(value, decimal.Decimal):
            s = str(value.normalize())
            p.Value = value
            p.Precision = len(s)
    
            point = s.find('.')
            if point == -1:
                p.NumericScale = 0
            else:
                p.NumericScale = len(s)-point
            
        else:
            # For any other type, just set the value and let pythoncom do the right thing.
            p.Value = value
        
        # Use -1 instead of 0 for empty strings and buffers
        if p.Size == 0: 
            p.Size = -1

    def _executeHelper(self, operation, isStoredProcedureCall, parameters=None):
        if self.connection is None:
            self._raiseCursorError(Error, None)
            return

        _parameter_error_message = ''

        try:
            self._new_command(isStoredProcedureCall)
            if parameters is not None:
                parameter_replacements = list()
                for i, value in enumerate(parameters):
                    if value is None:
                        parameter_replacements.append('NULL')
                        continue

                    # Otherwise, process the non-NULL parameter.
                    parameter_replacements.append('?')
                    p = self.cmd.CreateParameter('p%i' % i, _ado_type(value))
                    try:
                        self._configure_parameter(p, value)
                    except:
                        _parameter_error_message = u'Converting Parameter %s: %s, %s\n' %\
                            (p.Name, ado_type_name(p.Type), repr(value))
                        raise
                    self.cmd.Parameters.Append(p)

                # Use literal NULLs in raw queries, but not sproc calls.                            
                if not isStoredProcedureCall:
                    operation = operation % tuple(parameter_replacements)

            self.cmd.CommandText = operation
            recordset = self.cmd.Execute()

        except:
            import traceback
            stack_trace = u'\n'.join(traceback.format_exception(*sys.exc_info()))
            
            ado_params = u''
            if self.cmd:
                ado_params = format_parameters(self.cmd.Parameters)
            
            new_error_message = u'%s\n%s\nCommand: "%s"\nParameters: %s \nValues: %s' %\
            	(stack_trace, _parameter_error_message, operation, ado_params, parameters)
            self._raiseCursorError(DatabaseError, new_error_message)
            return

        self.rowcount=recordset[1]  # May be -1 if NOCOUNT is set.
        self._description_from_recordset(recordset[0])

        if isStoredProcedureCall and parameters != None:
            return self._returnADOCommandParameters(self.cmd)

    def execute(self, operation, parameters=None):
        "Prepare and execute a database operation (query or command)."
        self.messages = []
        self._executeHelper(operation, False, parameters)

    def executemany(self, operation, seq_of_parameters):
        """Execute the given command against all parameter sequences or mappings given in seq_of_parameters."""
        self.messages = []
        total_recordcount = 0

        for params in seq_of_parameters:
            self.execute(operation, params)
            if self.rowcount == -1:
                total_recordcount = -1
                
            if total_recordcount != -1:
                total_recordcount += self.rowcount

        self.rowcount = total_recordcount
        
    def _column_types(self):
        return [column_desc[1] for column_desc in self.description]

    def _fetch(self, rows=None):
        """Fetch rows from the current recordset.
        
        rows -- Number of rows to fetch, or None (default) to fetch all rows.
        """
        if self.connection is None or self.rs is None:
            self._raiseCursorError(Error, None)
            return
            
        if self.rs.State == adStateClosed or self.rs.BOF or self.rs.EOF:
            if rows == 1: # fetchone can return None
                return None
            else: # fetchall and fetchmany return empty lists
                return list()

        if rows:
            ado_results = self.rs.GetRows(rows)
        else:
            ado_results = self.rs.GetRows()

        py_columns = list()
        for ado_type, column in zip(self._column_types(), ado_results):
            py_columns.append( [_convert_to_python(cell, ado_type) for cell in column] )

        return tuple(zip(*py_columns))

    def fetchone(self):
        """Fetch the next row of a query result set, returning a single sequence, or None when no more data is available.

        An Error (or subclass) exception is raised if the previous call to executeXXX()
        did not produce any result set or no call was issued yet.
        """
        self.messages = []
        result = self._fetch(1)
        if result: # return record (not list of records)
            return result[0]
        return None

    def fetchmany(self, size=None):
        """Fetch the next set of rows of a query result, returning a list of tuples. An empty sequence is returned when no more rows are available."""
        self.messages = []
        if size is None:
            size = self.arraysize
        return self._fetch(size)

    def fetchall(self):
        """Fetch all remaining rows of a query result, returning them as a sequence of sequences."""
        self.messages = []
        return self._fetch()

    def nextset(self):
        """Skip to the next available recordset, discarding any remaining rows from the current recordset.

        If there are no more sets, the method returns None. Otherwise, it returns a true
        value and subsequent calls to the fetch methods will return rows from the next result set.
        """
        self.messages = []
        if self.connection is None or self.rs is None:
            self._raiseCursorError(Error, None)
            return None

        try:
            recordset = self.rs.NextRecordset()[0]
            if recordset is not None:
                self._description_from_recordset(recordset)
                return True
        except pywintypes.com_error, e:
            self._raiseCursorError(NotSupportedError, e.args)

    def setinputsizes(self,sizes): pass
    def setoutputsize(self, size, column=None): pass

# Type specific constructors as required by the DB-API 2 specification.
Date = datetime.date
Time = datetime.time
Timestamp = datetime.datetime
Binary = buffer

def DateFromTicks(ticks):
    """Construct an object holding a date value from the given # of ticks."""
    return Date(*time.localtime(ticks)[:3])

def TimeFromTicks(ticks):
    """Construct an object holding a time value from the given # of ticks."""
    return Time(*time.localtime(ticks)[3:6])

def TimestampFromTicks(ticks):
    """Construct an object holding a timestamp value from the given # of ticks."""
    return Timestamp(*time.localtime(ticks)[:6])

adoIntegerTypes = (adInteger,adSmallInt,adTinyInt,adUnsignedInt,adUnsignedSmallInt,adUnsignedTinyInt,adError)
adoRowIdTypes = (adChapter,)
adoLongTypes = (adBigInt, adUnsignedBigInt, adFileTime)
adoExactNumericTypes = (adDecimal, adNumeric, adVarNumeric, adCurrency)
adoApproximateNumericTypes = (adDouble, adSingle)
adoStringTypes = (adBSTR,adChar,adLongVarChar,adLongVarWChar,adVarChar,adVarWChar,adWChar,adGUID)
adoBinaryTypes = (adBinary, adLongVarBinary, adVarBinary)
adoDateTimeTypes = (adDBTime, adDBTimeStamp, adDate, adDBDate)

# Required DBAPI type specifiers
STRING   = _DbType(adoStringTypes)
BINARY   = _DbType(adoBinaryTypes)
NUMBER   = _DbType((adBoolean,) + adoIntegerTypes + adoLongTypes + adoExactNumericTypes + adoApproximateNumericTypes)
DATETIME = _DbType(adoDateTimeTypes)
# Not very useful for SQL Server, as normal row ids are usually just integers.
ROWID    = _DbType(adoRowIdTypes)


# Mapping ADO data types to Python objects.
def _convert_to_python(variant, adType):
    if variant is None: 
        return None
    return _variantConversions[adType](variant)

def _cvtCurrency((hi, lo), decimal_places=2):
    if lo < 0:
        lo += (2L ** 32)
    return decimal.Decimal((long(hi) << 32) + lo)/decimal.Decimal(1000)

def _cvtNumeric(variant):
	return _convertNumberWithCulture(variant, decimal.Decimal)

def _cvtFloat(variant):
    return _convertNumberWithCulture(variant, float)
        
def _convertNumberWithCulture(variant, f):
    try: 
        return f(variant)
    except (ValueError,TypeError):
        try:
            europeVsUS = str(variant).replace(",",".")
            return f(europeVsUS)
        except (ValueError,TypeError): pass

def _cvtComDate(comDate):
    date_as_float = float(comDate)
    day_count = int(date_as_float)
    fraction_of_day = abs(date_as_float - day_count)
    
    return (datetime.datetime.fromordinal(day_count + _ordinal_1899_12_31) +
        datetime.timedelta(milliseconds=fraction_of_day * _milliseconds_per_day))

_variantConversions = MultiMap({
        adoDateTimeTypes : _cvtComDate,
        adoApproximateNumericTypes: _cvtFloat,
        (adCurrency,): _cvtCurrency,
        (adBoolean,): bool,
        adoLongTypes+adoRowIdTypes : long,
        adoIntegerTypes: int,
        adoBinaryTypes: buffer, }
    , lambda x: x)


# Mapping Python data types to ADO type codes
def _ado_type(data):
    if isinstance(data, basestring):
        return adBSTR
    return _map_to_adotype[type(data)]

_map_to_adotype = {
    buffer: adBinary,
    float: adDouble,
    int: adInteger,
    long: adBigInt,
    bool: adBoolean,
    decimal.Decimal: adNumeric,
    datetime.date: adDate,
    datetime.datetime: adDate,
    datetime.time: adDate, }
