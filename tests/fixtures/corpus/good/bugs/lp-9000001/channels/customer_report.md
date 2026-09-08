# What we are seeing

After our cloud team ran the scheduled charm upgrade on Saturday night, new virtual machines come
up but never finish their first boot properly. They sit at the console showing repeated attempts to
reach the "instance data service" and eventually give up. SSH keys are not installed, so we cannot
log in to any machine created since the maintenance window.

Machines that were already running before the weekend are completely fine. We rebooted a couple of
them to check and they came back healthy, so it seems to only affect brand new ones.

This is blocking our release train because our CI creates fresh builders on every pipeline run and
none of them can pull their startup configuration. We have about forty pipelines queued.

We did not change anything on our side. The only thing that happened was the upgrade.
