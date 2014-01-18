"""Dowser - cherrypy site tracking the objects in the program."""
__import__("pkg_resources").declare_namespace(__name__)

import cgi
import gc
import os
import pkg_resources
localDir = os.path.dirname(pkg_resources.resource_filename(__name__, "main.css"))
from cStringIO import StringIO
from collections import defaultdict
import sys
import threading
import time
from types import FrameType, ModuleType

from PIL import Image
from PIL import ImageDraw

import cherrypy

import dowser.reftree

try:
    from pympler.asizeof import asizeof
except ImportError:
    def getsize(_):
        """Getsize stub."""
        return 0
else:
    def getsize(obj):
        """Safe asizeof to avoid errors on non-measureable objects."""
        try:
            return asizeof(obj)
        except BaseException:
            return 0


def format_size(size):
    if size == 0:
        return '<a href="{}">Unknown size</a>'.format(url("calc_sizes/"))
    elif size < (1 << 10):
        return str(size)
    elif size < (1 << 20):
        return str(size >> 10) + ' Kb'
    elif size < (1 << 30):
        return str(size >> 20) + ' Mb'
    elif size < (1 << 40):
        return str(size >> 30) + ' Gb'
    else:
        return str(size)


def get_repr(obj, limit=250):
    return cgi.escape(dowser.reftree.get_repr(obj, limit))


class _(object):
    pass


dictproxy = type(_.__dict__)

method_types = [type(tuple.__le__),                 # 'wrapper_descriptor'
                type([1].__le__),                   # 'method-wrapper'
                type(sys.getcheckinterval),         # 'builtin_function_or_method'
                type(cgi.FieldStorage.getfirst),    # 'instancemethod'
                ]


def url(path):
    try:
        return cherrypy.url(path)
    except AttributeError:
        return path


def template(name, **params):
    p = {'maincss': url("/main.css"),
         'home': url("/"),
         }
    p.update(params)
    return open(os.path.join(localDir, name)).read() % p


def handle_error():
    cherrypy.response.status = 500
    cherrypy.response.body = ["<html><body><h1>Sorry, an error occured</h1><div class='error'><pre>{}</pre></div></body></html>".format(cherrypy._cperror.format_exc())]


class Root(object):
    """Main object which is binded to cherrypy. It does all the processing."""

    period = 5
    maxhistory = 300
    _cp_config = {'request.error_response': handle_error}

    def __init__(self):
        self.running = False
        self.history = {}
        self.typesizes = {}
        self.samples = 0
        if cherrypy.__version__ >= '3.1':
            cherrypy.engine.subscribe('exit', self.stop)
        self.runthread = threading.Thread(target=self.start)
        self.runthread.start()

    def start(self):
        """Running in separate thread, update the statistics"""
        self.running = True
        while self.running:
            self.tick()
            time.sleep(self.period)

    def tick(self):
        """Internal loop updating objects statistics."""
        gc.collect()

        typecounts = defaultdict(int)
        for obj in gc.get_objects():
            objtype = type(obj)
            typecounts[objtype] += 1

        for objtype, count in typecounts.iteritems():
            typename = objtype.__module__ + "." + objtype.__name__
            if typename not in self.history:
                self.history[typename] = [0] * self.samples
            self.history[typename].append(count)

        samples = self.samples + 1

        # Add dummy entries for any types which no longer exist
        for typename, hist in self.history.iteritems():
            diff = samples - len(hist)
            if diff > 0:
                hist.extend([0] * diff)

        # Truncate history to self.maxhistory
        if samples > self.maxhistory:
            for typename, hist in self.history.iteritems():
                hist.pop(0)
        else:
            self.samples = samples

    def stop(self):
        """Stop the execution."""
        self.running = False

    def index(self, floor=0):
        """Main page."""
        rows = []
        typenames = self.history.keys()
        typenames.sort()
        for typename in typenames:
            hist = self.history[typename]
            maxhist = max(hist)
            if maxhist > int(floor):
                row = ('<div class="typecount">{typename}<br />'
                       '<img class="chart" src="{charturl}" /><br />'
                       'Min: {minuse} Cur: {curuse} Max: {maxuse} Size: {size} <a href="{traceurl}">TRACE</a></div>'
                       .format(typename=cgi.escape(typename),
                               charturl=url("chart/%s" % typename),
                               minuse=min(hist), curuse=hist[-1], maxuse=maxhist,
                               traceurl=url("trace/%s" % typename),
                               size=self.typesizes.get(typename, 'Unknown size')
                               )
                       )
                rows.append(row)
        return template("graphs.html", output="\n".join(rows))
    index.exposed = True

    def calc_sizes(self):
        """Calucalte total sizes of all the typenames."""
        gc.collect()

        _typesizes = defaultdict(int)
        for obj in gc.get_objects():
            _typesizes[type(obj)] += getsize(obj)

        typesizes = {}
        for objtype, size in _typesizes.iteritems():
            typename = objtype.__module__ + "." + objtype.__name__
            typesizes[typename] = format_size(size)

        del _typesizes
        self.typesizes = typesizes
        del typesizes

        return "Ok"

    calc_sizes.exposed = True

    def chart(self, typename):
        """Return a sparkline chart of the given type."""
        data = self.history[typename]
        height = 20.0
        scale = height / max(data)
        im = Image.new("RGB", (len(data), int(height)), 'white')
        draw = ImageDraw.Draw(im)
        draw.line([(i, int(height - (v * scale))) for i, v in enumerate(data)],
                  fill="#009900")
        del draw

        f = StringIO()
        im.save(f, "PNG")
        result = f.getvalue()

        cherrypy.response.headers["Content-Type"] = "image/png"
        return result
    chart.exposed = True

    def trace(self, typename, objid=None):
        gc.collect()

        if objid is None:
            rows = self.trace_all(typename)
        else:
            rows = self.trace_one(typename, objid)

        return template("trace.html", output="\n".join(rows),
                        typename=cgi.escape(typename),
                        objid=str(objid or ''))
    trace.exposed = True

    def trace_all(self, typename):
        rows = []
        for obj in gc.get_objects():
            objtype = type(obj)
            if objtype.__module__ + "." + objtype.__name__ == typename:
                rows.append("<p class='obj'>%s</p>"
                            % ReferrerTree(obj).get_repr(obj))
        if not rows:
            rows = ["<h3>The type you requested was not found.</h3>"]
        return rows

    def trace_one(self, typename, objid):
        rows = []
        objid = int(objid)
        all_objs = gc.get_objects()
        for obj in all_objs:
            if id(obj) == objid:
                objtype = type(obj)
                if objtype.__module__ + "." + objtype.__name__ != typename:
                    rows = ["<h3>The object you requested is no longer "
                            "of the correct type.</h3>"]
                else:
                    # Attributes
                    rows.append('<div class="obj"><h3>Attributes</h3>')
                    for k in dir(obj):
                        try:
                            v = getattr(obj, k)
                        except BaseException as e:
                            v = '<Unrepresentable attribute: {}>'.format(e)

                        if type(v) not in method_types:
                            rows.append('<p class="attr"><b>%s:</b> %s</p>' %
                                        (k, get_repr(v)))
                        del v
                    rows.append('</div>')

                    # Referrers
                    rows.append('<div class="refs"><h3>Referrers (Parents)</h3>')
                    rows.append('<p class="desc"><a href="%s">Show the '
                                'entire tree</a> of reachable objects</p>'
                                % url("/tree/%s/%s" % (typename, objid)))
                    tree = ReferrerTree(obj)
                    tree.ignore(all_objs)
                    for depth, parentid, parentrepr in tree.walk(maxdepth=1):
                        if parentid:
                            rows.append("<p class='obj'>%s</p>" % parentrepr)
                    rows.append('</div>')

                    # Referents
                    rows.append('<div class="refs"><h3>Referents (Children)</h3>')
                    for child in gc.get_referents(obj):
                        rows.append("<p class='obj'>%s</p>" % tree.get_repr(child))
                    rows.append('</div>')
                break
        if not rows:
            rows = ["<h3>The object you requested was not found.</h3>"]
        return rows

    def tree(self, typename, objid):
        gc.collect()

        rows = []
        objid = int(objid)
        all_objs = gc.get_objects()
        for obj in all_objs:
            if id(obj) == objid:
                objtype = type(obj)
                if objtype.__module__ + "." + objtype.__name__ != typename:
                    rows = ["<h3>The object you requested is no longer "
                            "of the correct type.</h3>"]
                else:
                    rows.append('<div class="obj">')

                    tree = ReferrerTree(obj)
                    tree.ignore(all_objs)
                    for depth, parentid, parentrepr in tree.walk(maxresults=1000):
                        rows.append(parentrepr)

                    rows.append('</div>')
                break
        if not rows:
            rows = ["<h3>The object you requested was not found.</h3>"]

        params = {'output': "\n".join(rows),
                  'typename': cgi.escape(typename),
                  'objid': str(objid),
                  }
        return template("tree.html", **params)
    tree.exposed = True


try:
    # CherryPy 3
    from cherrypy import tools
    Root.main_css = tools.staticfile.handler(root=localDir, filename="main.css")
except ImportError:
    # CherryPy 2
    cherrypy.config.update({
        '/': {'log_debug_info_filter.on': False},
        '/main.css': {
            'static_filter.on': True,
            'static_filter.file': 'main.css',
            'static_filter.root': localDir,
        },
    })


class ReferrerTree(dowser.reftree.Tree):

    ignore_modules = True

    def _gen(self, obj, depth=0):
        if self.maxdepth and depth >= self.maxdepth:
            yield depth, 0, "---- Max depth reached ----"
            raise StopIteration

        if isinstance(obj, ModuleType) and self.ignore_modules:
            raise StopIteration

        refs = gc.get_referrers(obj)
        refiter = iter(refs)
        self.ignore(refs, refiter)
        thisfile = sys._getframe().f_code.co_filename
        for ref in refiter:
            # Exclude all frames that are from this module or reftree.
            if (
                isinstance(ref, FrameType)
                and ref.f_code.co_filename in (thisfile, self.filename)
            ):
                continue

            # Exclude all functions and classes from this module or reftree.
            mod = getattr(ref, "__module__", "")
            if mod is not None and ("dowser" in mod or "reftree" in mod or mod == '__main__'):
                continue

            # Exclude all parents in our ignore list.
            if id(ref) in self._ignore:
                continue

            # Yield the (depth, id, repr) of our object.
            yield depth, 0, '%s<div class="branch">' % (" " * depth)
            if id(ref) in self.seen:
                yield depth, id(ref), "see %s above" % id(ref)
            else:
                self.seen[id(ref)] = None
                yield depth, id(ref), self.get_repr(ref, obj)

                for parent in self._gen(ref, depth + 1):
                    yield parent
            yield depth, 0, '%s</div>' % (" " * depth)

    def get_repr(self, obj, referent=None):
        """Return an HTML tree block describing the given object."""
        objtype = type(obj)
        typename = objtype.__module__ + "." + objtype.__name__
        prettytype = typename.replace("__builtin__.", "")

        name = getattr(obj, "__name__", "")
        if name:
            prettytype = "%s %r" % (prettytype, name)

        key = ""
        if referent:
            key = self.get_refkey(obj, referent)
        objsize = format_size(getsize(obj))
        return ('<a class="objectid" href="{objurl}">{objid}</a> '
                '<span class="typename">{prettytype}</span>{key} &mdash; {objsize}<br />'
                '<span class="repr">{desc}</span>'
                .format(objurl=url("/trace/%s/%s" % (typename, id(obj))),
                        objid=id(obj),
                        prettytype=prettytype,
                        key=key,
                        objsize=objsize,
                        desc=get_repr(obj, 100))
                )

    def get_refkey(self, obj, referent):
        """Return the dict key or attribute name of obj which refers to referent."""
        if isinstance(obj, dict):
            for k, v in obj.iteritems():
                if v is referent:
                    try:
                        return " (via its %r key)" % k
                    except TypeError:
                        return " (via its unrepresentable %s key)" % k.__class__.__name__

        for k in dir(obj) + ['__dict__']:
            if getattr(obj, k, None) is referent:
                try:
                    return " (via its %r attribute)" % k
                except TypeError:
                    return " (via its unrepresentable %s attribute)" % k.__class__.__name__
        return ""


def launch_memory_usage_server(port=8080):
    import cherrypy
    import dowser

    cherrypy.tree.mount(dowser.Root())
    cherrypy.config.update({
        'environment': 'embedded',
        'server.socket_port': port
    })

    cherrypy.engine.start()


def main():
    try:
        cherrypy.quickstart(Root())
    except AttributeError:
        cherrypy.root = Root()
        cherrypy.server.start()

if __name__ == '__main__':
    main()
