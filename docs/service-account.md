# Creating the WebLogic service account

The MCP server must not authenticate as the domain administrator. It gets its own account with
the minimum rights to do its job: read runtime state, and drive server lifecycle.

## Which groups, and why

WebLogic ships four global roles backed by default groups. Two matter here:

| Group | Grants |
|---|---|
| `Monitors` | Read-only view of configuration and runtime MBeans |
| `Operators` | Server lifecycle — start, stop, resume — plus the read access |

Put the account in **both**. `Operators` alone already covers the lifecycle calls; adding
`Monitors` makes the read intent explicit rather than incidental. Neither grants deployment or
configuration editing, which is the point.

## Create it

`scripts/create-mcp-user.py` is a WLST script. It is idempotent — re-running reports existing
membership instead of failing.

It expects four properties, supplied with `-loadProperties`:

```properties
username=weblogic          # a domain administrator, used only to create the account
password=...
mcp_username=wlsmcp        # the account being created
mcp_password=...
```

Generate a password that satisfies WebLogic's rule of at least 8 characters including a
non-alphabetic one:

```bash
printf 'Mcp%s1!\n' "$(openssl rand -hex 5)"
```

### If WebLogic runs directly on a host

```bash
"$ORACLE_HOME/oracle_common/common/bin/wlst.sh" -skipWLSModuleScanning \
  -loadProperties /path/to/mcp-user.properties \
  scripts/create-mcp-user.py
```

Delete the properties file afterwards. Better still, create it with `umask 077` in a
`mktemp` location so it is never world-readable in the first place.

### If WebLogic runs in a container

Stream both files in over stdin rather than using `docker cp`. `docker cp` writes files as
`root`, and WebLogic containers run as a non-root user — which leaves a credentials file inside
the container that the server process cannot delete:

```bash
CONTAINER=your-weblogic-container

docker exec -i "$CONTAINER" bash -c 'cat > /tmp/create-mcp-user.py' < scripts/create-mcp-user.py
printf 'username=%s\npassword=%s\nmcp_username=%s\nmcp_password=%s\n' \
  "$ADMIN_USER" "$ADMIN_PASS" "$MCP_USER" "$MCP_PASS" \
| docker exec -i "$CONTAINER" bash -c 'umask 077; cat > /tmp/mcp-user.properties'

docker exec "$CONTAINER" wlst.sh -skipWLSModuleScanning \
  -loadProperties /tmp/mcp-user.properties /tmp/create-mcp-user.py

docker exec "$CONTAINER" rm -f /tmp/mcp-user.properties /tmp/create-mcp-user.py
```

Expected output:

```
created user 'wlsmcp'
added 'wlsmcp' to 'Operators'
added 'wlsmcp' to 'Monitors'
verify: member of Operators    -> True
verify: member of Monitors     -> True
```

> `listMemberGroups()` returns an opaque cursor handle, not a list — printing it gives you
> something useless like `Cursor_4`. The script verifies membership with `isMember(group, user, 1)`
> instead.

## Prove the boundary

Claiming least privilege is worth nothing without showing where the wall is. As the new account:

```bash
B=http://your-admin-host:7001/management/weblogic/latest

# allowed
curl -su wlsmcp:$PASS "$B/domainRuntime/serverLifeCycleRuntimes?fields=name,state&links=none"
curl -su wlsmcp:$PASS "$B/domainRuntime/serverRuntimes/AdminServer/JVMRuntime?links=none"

# denied - 403
curl -su wlsmcp:$PASS -H 'X-Requested-By: t' -H 'Content-Type: application/json' \
  -X POST -d '{"name":"demoDS"}' "$B/edit/JDBCSystemResources"
```

| Operation | Result |
|---|---|
| Read `serverLifeCycleRuntimes`, `serverRuntimes`, `JVMRuntime` | 200 |
| `POST serverLifeCycleRuntimes/{server}/start` | 200 |
| `POST edit/changeManager/startEdit` | **403** — "Failed to start an edit session." |
| `POST edit/JDBCSystemResources` | **403** |

## One thing that surprises people

`Operators` **can stop the Administration Server**. That is WebLogic's design, not a
misconfiguration. Stopping it also stops the REST API this MCP server depends on, so nothing here
can start it again.

This is why the server adds its own confirmation gate on top of the WebLogic role: group
membership alone is not a sufficient guardrail. See `WLS_ALLOW_ADMIN_SHUTDOWN` in the README.
