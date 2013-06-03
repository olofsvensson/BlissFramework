import os
import re
import sys
import optparse
import gevent
import gevent.queue
import bottle
import socket
import time
import gevent
import logging
import code
import rlcompleter
import pprint
import traceback
import cStringIO

HISTORY = {}
LOG = {}
OUTPUT = {}
QUEUE = {}
CODE_EXECUTION = {}
ROOT_PATH = os.path.dirname(os.path.abspath(__file__))
INTERPRETER = None

# patch socket module;
# by default bottle doesn't set address as reusable
# and there is no option to do it...
socket.socket._bind = socket.socket.bind
def my_socket_bind(self, *args, **kwargs):
  self.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
  return socket.socket._bind(self, *args, **kwargs)
socket.socket.bind = my_socket_bind

class InteractiveInterpreter(code.InteractiveInterpreter):
  def __init__(self, globals_dict=None):
    if globals_dict is None:
      globals_dict = globals()
    code.InteractiveInterpreter.__init__(self, globals_dict)

    self.at_prompt = True
    self.completer = rlcompleter.Completer(globals_dict)
    self.error = cStringIO.StringIO()

  def write(self, data):
    self.error.write(data)

  def runcode(self, c):
    try:
      exec c in self.locals
    except KeyboardInterrupt:
      self.showtraceback()
    except SystemExit:
      # maybe a self.showtraceback() would be good?
      raise
    except:
      self.showtraceback()
    else:
      if code.softspace(sys.stdout, 0):
         print

  def compile_and_run(self, python_code_to_execute, stdout, dontcompile=False):
    code_obj = None
    self.at_prompt = False

    try:
      if dontcompile:
        with stdout:
          self.runcode(python_code_to_execute)
      else:
        try:
          code_obj = code.compile_command(python_code_to_execute)
        except SyntaxError, exc_instance:
          raise RuntimeError, str(exc_instance)
        else:
          if code_obj is None:
            # input is incomplete
            raise EOFError
          else:
            with stdout:
              self.runcode(code_obj)

            if self.error.tell() > 0:
              error_string = self.error.getvalue()
              self.error = cStringIO.StringIO()
              raise RuntimeError, error_string
    finally:
      self.at_prompt = True

def MyLogHandler(client_id):
  try:
    log_handler = LOG[client_id]
  except KeyError:
    log_handler = _MyLogHandler(client_id)
    log_handler.setLevel(logging.DEBUG)
    logging.getLogger().addHandler(log_handler)
    LOG[client_id]=log_handler

  return log_handler
  
class _MyLogHandler(logging.Handler):
  def __init__(self, client_id):
    logging.Handler.__init__(self)
    self.queue = QUEUE[client_id]
    
  def emit(self, record):
    self.queue.put(("log_record", record.getMessage())) 

def MyStdout(client_id):
  return OUTPUT.setdefault(client_id, _MyStdout(client_id))
     
class _MyStdout:
  def __init__(self, client_id):
    self.client_id = client_id
    self.queue = QUEUE[client_id]

  def __enter__(self):
    sys.stdout = self

  def __exit__(self, *args):
    sys.stdout = sys.__stdout__

  def write(self, output):
    self.queue.put(("output", output))
    sys.__stdout__.write('[client_id %s] %d bytes output: %r\n' % (self.client_id, len(output), output))

@bottle.route("/output/:client_id")
def send_output(client_id):
  queue = QUEUE[client_id]
  while True:
      item, data = queue.get()
      if item == "output":
        yield '<script type="text/javascript">window.parent.display_output(%r);</script>' % data
      else:
        yield '<script type="text/javascript">window.parent.display_log(%r);</script>' % data

@bottle.get("/history_request")
def send_history():
  client_id = bottle.request.GET["client_id"]
  return HISTORY.get(client_id, [])

@bottle.get("/completion_request")
def send_completion():
  text = bottle.request.GET["text"]
  tmp = filter(None, re.split(r'[ ;]', text))
  if len(tmp) > 0:
    text = tmp[-1]
  else:
    text = ""
  completion_start_index = text.rfind(text)
  possibilities = []
  i = 0
  while True:
    possibility = INTERPRETER.completer.complete(text, i)
    if possibility is None:
      break
    else:
      if len(possibilities) > 0 and possibility == possibilities[0]:
        break
      possibilities.append(possibility)
    i += 1
    if len(possibilities) == 1:
      cmd = text[:completion_start_index]+possibilities[0]
    else:
      cmd = text
    return { "possibilities":possibilities, "cmd": cmd }
    
@bottle.get("/command")
def execute_command():
  client_id = bottle.request.GET["client_id"]
  code = bottle.request.GET["code"]

  try:
    python_code_to_execute = str(code).strip()+"\n"
  except UnicodeEncodeError: 
    python_code_to_execute = ""

  if len(python_code_to_execute) == 0:
    return {"error":""}
  elif python_code_to_execute == "__CTRLC__\n":
    try:
      CODE_EXECUTION[client_id].kill(exception=KeyboardInterrupt)
    except KeyError:
      return {"error":"CTRLC"}
  else:
    sys.__stdout__.write("storing command %r for history for client %s\n" % (python_code_to_execute, client_id))
    HISTORY.setdefault(client_id, []).append(python_code_to_execute)
    mystdout = MyStdout(client_id)
    CODE_EXECUTION[client_id] = gevent.spawn(do_execute, python_code_to_execute, mystdout)
    res=CODE_EXECUTION[client_id].get()
    time.sleep(0.02) #let time for output to be flushed
    return res

def do_execute(python_code_to_execute, mystdout):
    try:
      INTERPRETER.compile_and_run(python_code_to_execute, mystdout)
    except EOFError:
      return {"error":"EOF","input":python_code_to_execute}
    except RuntimeError, e:
      error_string = str(e)
      sys.stderr.write(error_string)
      return {"error":error_string}
    else:
      return {"error":""}

@bottle.route('/')
def main():
  contents = file(os.path.join(ROOT_PATH, "terminal.html"), "r")
  client_id = str(id(contents))
  QUEUE[client_id]=gevent.queue.Queue()
  MyLogHandler(client_id)
  MyStdout(client_id)
  return contents.read() % ((id(contents), )*2) 

@bottle.route("/lib/CodeMirror-2.3/lib/:filename")
def send_static_codemirror(filename):
  return bottle.static_file(filename, root=os.path.join(ROOT_PATH, "lib/CodeMirror-2.3/lib"))

@bottle.route("/lib/CodeMirror-2.3/mode/python/:filename")
def send_static_codemirror_py(filename):
  return bottle.static_file(filename, root=os.path.join(ROOT_PATH, "lib/CodeMirror-2.3/mode/python"))

@bottle.route("/images/silk/:filename")
def send_static_silk_images(filename):
  return bottle.static_file(filename, root=os.path.join(ROOT_PATH, "images/silk"))

@bottle.route('/:directory/:filename')
def send_static(directory, filename):
  return bottle.static_file(filename, root=os.path.join(ROOT_PATH, directory))

def serve_forever(port=None, monkey=False):
  bottle.run(server="gevent", host="", port=port, monkey=monkey, quiet=True)

def set_interpreter(interpreter_object):
  global INTERPRETER
  INTERPRETER = interpreter_object


if __name__=="__main__":
    usage = "usage: \%prog [-p<port>]"
    
    parser = optparse.OptionParser(usage)
    parser.add_option('-p', '--port', dest='port', type='int',
                      help='Port to listen on (default 8099)', default=8099, action='store')
    
    options, args = parser.parse_args()

    logging.basicConfig()
    logging.getLogger().setLevel(logging.DEBUG)

    set_interpreter(InteractiveInterpreter())
    serve_forever(options.port, monkey=True)
    
