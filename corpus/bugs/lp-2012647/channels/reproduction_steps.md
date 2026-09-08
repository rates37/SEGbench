1. Deploy keystone with `action-managed-upgrade=true`.
2. Deploy keystone-ldap (or another domain-backend provider) and relate it to keystone.
3. Point keystone at the next OpenStack release series.
4. Pause the keystone unit.
5. Run the `openstack-upgrade` action.
