Overview
========

This is a slightly modified version of dowser from http://e-mats.org/2013/01/debugging-pythons-memory-usage-with-dowser/

Changes:

* Cherrypy integration replaced with aiohttp
* Python 2 is not supported anymore. Python 3 only (you should expect it with aiohttp)
* Added support for pympler to find heavy objects and measure object sizes
* Added support for tracemalloc to find memory allocations
* Added option to search only for types having at least N instances
* Added option to sort object types by instance count or total size
* Fixed displaying object info when its attributes cannot be represented

Usage
-----

To integrate dowser to your aiohttp server, use:

    import dowser
    dowser.setup(existing_app)

This will bind sub-path `/dowser/` to your existing aiohttp app.
Alternatively one can specify custom sub-path:

    dowser.setup(existing_app, bind_path='/.secret-dowser/')
