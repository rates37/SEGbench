Follow-up investigation from another engineer on the same report:

The port in the traceback (35347 in this run, 35337 has also been seen) is served by apache2 on
the unit, not haproxy — it's the WSGI vhost for the Keystone admin API
(`/etc/apache2/sites-enabled/wsgi-openstack-api.conf`, a `WSGIDaemonProcess keystone-admin`
listening on that port). That process is intentionally not running while the unit is paused.

As a workaround, resuming both the hacluster and keystone units and then manually re-triggering
the `config-changed-postupgrade` hook lets it finish without error — consistent with the failing
call needing the local API, which is only up once the unit is resumed.
