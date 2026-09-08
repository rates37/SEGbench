Our virtual machines stopped working after the upgrade.

Our cloud provider told us what the problem is: the reconnection path rebuilds the chassis
registration without repopulating the cached datapath mapping, so every proxy spawned afterwards
binds an empty datapath and answers on the wrong socket path.
