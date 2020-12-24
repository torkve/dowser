"""Dowser - aiohttp site tracking the objects in the program."""

import gc
import os
import cgi
import sys
import time
import html
import threading
import traceback
from io import BytesIO, StringIO
from types import FrameType, ModuleType
from collections import defaultdict

from PIL import Image
from PIL import ImageDraw

import pkgutil
import aiohttp.web

import dowser.reftree


try:
    from pympler.asizeof import asizeof
except ImportError:
    pympler_available = False
else:
    def getsize(obj):
        """Safe asizeof to avoid errors on non-measureable objects."""
        try:
            return asizeof(obj)
        except BaseException:
            return 0

    pympler_available = True

try:
    import tracemalloc
    tracemalloc_available = True
except ImportError:
    tracemalloc_available = False


@aiohttp.web.middleware
async def handle_error(request, handler):
    try:
        return await handler(request)
    except aiohttp.web.HTTPNotFound:
        raise
    except Exception:
        return aiohttp.web.Response(
            status=500,
            content_type='text/html',
            text=f"<html><body><h1>Sorry, an error occured</h1><div class='error'><pre>{traceback.format_exc()}</pre></div></body></html>",
        )


dowser_blueprint = aiohttp.web.Application(middlewares=[handle_error])


def unknown_size():
    if pympler_available:
        return f'<a href="{url("calc_sizes")}">Unknown size</a>'
    else:
        return ''


def format_size(size):
    if size == 0:
        return unknown_size()
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
    return html.escape(dowser.reftree.get_repr(obj, limit))


class _(object):
    pass


dictproxy = type(_.__dict__)

method_types = [type(tuple.__le__),                 # 'wrapper_descriptor'
                type([1].__le__),                   # 'method-wrapper'
                type(sys.getcheckinterval),         # 'builtin_function_or_method'
                type(cgi.FieldStorage.getfirst),    # 'instancemethod'
                ]


def static(path):
    return pkgutil.get_data(__package__, path.lstrip('/'))


def url(path, **kwargs):
    try:
        return dowser_blueprint.router[path].url_for(**kwargs)
    except KeyError:
        return path


def make_static_handler(path, content_type='text/html'):
    async def handler(request, path=path, content_type=content_type):
        return aiohttp.web.Response(body=static(path), content_type=content_type)

    return handler


dowser_blueprint.add_routes([
    aiohttp.web.get(u, make_static_handler(u, ct), name=u.lstrip('/'))
    for u, ct in (
        ('/graphs.html', 'text/html'),
        ('/main.css', 'text/css'),
        ('/trace.html', 'text/html'),
        ('/tracemalloc.html', 'text/html'),
        ('/tree.html', 'text/html'),
    )
])


def template(name, **params):
    p = {'maincss': url("main.css"),
         'home': url("index"),
         }
    p.update(params)
    return aiohttp.web.Response(content_type='text/html', text=(static(name).decode() % p))


class Root:
    """Main object which is bound to aiohttp. It does all the processing."""

    period = 5
    maxhistory = 300

    def __init__(self):
        self.running = False
        self.history = {}
        self.typesizes = {}
        self.samples = 0

    async def start(self, app):
        self.runthread = threading.Thread(target=self._start)
        self.runthread.start()

    def _start(self):
        """Running in separate thread, update the statistics"""
        self.running = True
        while self.running:
            self.tick()
            time.sleep(self.period)

    def mount_to(self, app):
        if tracemalloc_available:
            app.add_routes([
                aiohttp.web.get(r'/tracemalloc/', self.tracemalloc, name='tracemalloc'),
                aiohttp.web.get(r'/tracemalloc', self.tracemalloc),
            ])

        if pympler_available:
            app.add_routes([
                aiohttp.web.get(r'/calc_sizes/', self.calc_sizes, name='calc_sizes'),
                aiohttp.web.get(r'/calc_sizes', self.calc_sizes),
            ])

        app.add_routes([
            aiohttp.web.get(r'/tree/{typename}/{objid}', self.tree, name='tree'),
            aiohttp.web.get(r'/trace/{typename}/{objid}', self.trace, name='trace_objid'),
            aiohttp.web.get(r'/trace/{typename}', self.trace, name='trace'),
            aiohttp.web.get(r'/chart/{typename}', self.chart, name='chart'),
            aiohttp.web.get(r'/', self.index, name='index'),
        ])

        dowser_blueprint.on_startup.append(self.start)
        dowser_blueprint.on_shutdown.append(self.stop)

    def tick(self):
        """Internal loop updating objects statistics."""
        gc.collect()

        typecounts = defaultdict(int)
        for obj in gc.get_objects():
            objtype = type(obj)
            typecounts[objtype] += 1

        for objtype, count in typecounts.items():
            typename = objtype.__module__ + "." + objtype.__name__
            if typename not in self.history:
                self.history[typename] = [0] * self.samples
            self.history[typename].append(count)

        samples = self.samples + 1

        # Add dummy entries for any types which no longer exist
        for typename, hist in self.history.items():
            diff = samples - len(hist)
            if diff > 0:
                hist.extend([0] * diff)

        # Truncate history to self.maxhistory
        if samples > self.maxhistory:
            for typename, hist in self.history.items():
                hist.pop(0)
        else:
            self.samples = samples

    async def stop(self, app):
        """Stop the execution."""
        self.running = False

    async def tracemalloc(self, request):
        limit = int(request.query.get('limit', 50))

        io = StringIO()
        t = tracemalloc.DisplayTop(limit, file=io)
        t.filename_parts = 10
        t.show_lineno = True
        t.display()
        return template("tracemalloc.html", output=io.getvalue())

    async def index(self, request):
        """Main page."""
        floor = int(request.query.get('floor', 0))

        rows = []
        typenames = list(self.history.keys())
        typenames.sort()
        for typename in typenames:
            hist = self.history[typename]
            maxhist = max(hist)
            if maxhist > int(floor):
                size = 'Size: <span class="objsize">{}</span>'.format(self.typesizes.get(typename, unknown_size())) if pympler_available else ''
                row = ('<div class="typecount"><span class="typename">{typename}</span><br />'
                       '<img class="chart" src="{charturl}" /><br />'
                       'Min: <span class="minuse">{minuse}</span> Cur: <span class="curuse">{curuse}</span> Max: <span class="maxuse">{maxuse}</span> {size} <a href="{traceurl}">TRACE</a></div>'
                       .format(typename=html.escape(typename),
                               charturl=url("chart", typename=typename),
                               minuse=min(hist), curuse=hist[-1], maxuse=maxhist,
                               traceurl=url("trace", typename=typename),
                               size=size,
                               )
                       )
                rows.append(row)
        return template("graphs.html", output="\n".join(rows), floor=int(floor))

    async def calc_sizes(self, request):
        """Calucalte total sizes of all the typenames."""
        gc.collect()

        _typesizes = defaultdict(int)
        for obj in gc.get_objects():
            _typesizes[type(obj)] += getsize(obj)

        typesizes = {}
        for objtype, size in _typesizes.items():
            typename = objtype.__module__ + "." + objtype.__name__
            typesizes[typename] = format_size(size)

        del _typesizes
        self.typesizes = typesizes
        del typesizes

        return aiohttp.web.Response(text="Ok")

    async def chart(self, request):
        """Return a sparkline chart of the given type."""
        typename = request.match_info['typename']

        data = self.history[typename]
        height = 20.0
        scale = height / max(data)
        im = Image.new("RGB", (len(data), int(height)), 'white')
        draw = ImageDraw.Draw(im)
        draw.line([(i, int(height - (v * scale))) for i, v in enumerate(data)],
                  fill="#009900")
        del draw

        f = BytesIO()
        im.save(f, "PNG")
        result = f.getvalue()

        return aiohttp.web.Response(content_type='image/png', body=result)

    async def trace(self, request):
        typename = request.match_info['typename']
        objid = request.match_info.get('objid')

        gc.collect()

        if objid is None:
            rows = self.trace_all(typename)
        else:
            rows = self.trace_one(typename, objid)

        return template("trace.html", output="\n".join(rows),
                        typename=html.escape(typename),
                        objid=str(objid or ''))

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
                            v = f'<Unrepresentable attribute: {e}>'

                        if type(v) not in method_types:
                            rows.append(f'<p class="attr"><b>{k}:</b> {get_repr(v)}</p>')
                        del v
                    rows.append('</div>')

                    # Referrers
                    rows.append('<div class="refs"><h3>Referrers (Parents)</h3>')
                    rows.append('<p class="desc"><a href="%s">Show the '
                                'entire tree</a> of reachable objects</p>'
                                % url("tree", typename=typename, objid=str(objid)))
                    tree = ReferrerTree(obj)
                    tree.ignore(all_objs)
                    for depth, parentid, parentrepr in tree.walk(maxdepth=1):
                        if parentid:
                            rows.append(f"<p class='obj'>{parentrepr}</p>")
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

    async def tree(self, request):
        typename = request.match_info['typename']
        objid = request.match_info['objid']

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
                  'typename': html.escape(typename),
                  'objid': str(objid),
                  }
        return template("tree.html", **params)


class ReferrerTree(dowser.reftree.Tree):
    ignore_modules = True

    def _gen(self, obj, depth=0):
        if self.maxdepth and depth >= self.maxdepth:
            yield depth, 0, "---- Max depth reached ----"
            return

        if isinstance(obj, ModuleType) and self.ignore_modules:
            return

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
                yield depth, id(ref), f"see {id(ref)} above"
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

        objsize = ' &mdash; {}'.format(format_size(getsize(obj))) if pympler_available else ''
        objid = str(id(obj))
        objurl = url("trace_objid", typename=typename, objid=str(objid))
        return (f'<a class="objectid" href="{objurl}">{objid}</a> '
                f'<span class="typename">{prettytype}</span>{key}{objsize}<br />'
                f'<span class="repr">{get_repr(obj, 100)}</span>'
                )

    def get_refkey(self, obj, referent):
        """Return the dict key or attribute name of obj which refers to referent."""
        if isinstance(obj, dict):
            for k, v in obj.items():
                if v is referent:
                    try:
                        return f" (via its {k!r} key)"
                    except TypeError:
                        return f" (via its unrepresentable {k.__class__.__name__} key)"

        for k in dir(obj) + ['__dict__']:
            if getattr(obj, k, None) is referent:
                try:
                    return f" (via its {k!r} attribute)"
                except TypeError:
                    return f" (via its unrepresentable {k.__class__.__name__} attribute)"
        return ""


dowser_instance = Root()
dowser_instance.mount_to(dowser_blueprint)


def setup(app: aiohttp.web.Application, **kwargs):
    if 'dowser' in app:
        return

    bind_path = kwargs.get('bind_path') or '/dowser/'
    app['dowser'] = {'bind_path': bind_path}
    app.add_subapp(bind_path, dowser_blueprint)
