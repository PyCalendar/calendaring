The CalDAV tests can run against every test server of caldav's own test
framework, ``caldav.testing`` (``pytest --caldav-servers=all``), in
parallel with ``pytest -n N --dist loadgroup``.
