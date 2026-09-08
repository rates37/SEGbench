## Summary

Following the documented paused-single-unit (and paused-single-unit-with-hacluster) upgrade
procedure, running the openstack-upgrade action on a paused keystone unit fails when the
application has a domain-backend relation (such as keystone-ldap): the package installation
succeeds, but the action then fails.

## Expected Behaviour

The action installs the new release and completes successfully while the unit remains paused.
The unit can then be resumed with services running the new release.

## Actual Behaviour

The package installation succeeds, but the action fails with a traceback (see the error trace
channel). It looks like the post-upgrade hook is trying to reach something on the keystone unit
itself that isn't available while the unit is paused.

Is the documented pause-then-upgrade procedure simply incompatible with having a domain-backend
relation, or is this a bug in the charm?
