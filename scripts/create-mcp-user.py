# v1.1 - changelog: print explicit group membership instead of the raw cursor handle.
# v1.0 - WLST online: create the least-privilege service account used by the MCP server.
# Idempotent: safe to re-run. Reads properties via -loadProperties.
import sys

admin_url  = os.environ.get("ADMIN_URL", "t3://localhost:7001")
groups     = os.environ.get("MCP_GROUPS", "Operators,Monitors").split(",")

try:
    connect(username, password, admin_url)
except:
    print("FATAL: could not connect to %s as %s" % (admin_url, username))
    sys.exit(1)

domainConfig()
atn = cmo.getSecurityConfiguration().getDefaultRealm().lookupAuthenticationProvider('DefaultAuthenticator')

if atn.userExists(mcp_username):
    print("user '%s' already exists - leaving password unchanged" % mcp_username)
else:
    atn.createUser(mcp_username, mcp_password, 'WebLogic MCP server service account')
    print("created user '%s'" % mcp_username)

for g in groups:
    g = g.strip()
    if not g:
        continue
    if atn.isMember(g, mcp_username, 1):
        print("'%s' already a member of '%s'" % (mcp_username, g))
    else:
        atn.addMemberToGroup(g, mcp_username)
        print("added '%s' to '%s'" % (mcp_username, g))

# listMemberGroups() returns an opaque cursor, not a list - verify membership explicitly
for g in groups:
    g = g.strip()
    if g:
        print("verify: member of %-12s -> %s" % (g, atn.isMember(g, mcp_username, 1)))
disconnect()
exit()
