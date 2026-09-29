# Threat model

This is the threat model of darkscope, which means the collector, the server and the panel,
and it says nothing about DarkFi itself. I wrote it for someone who is deciding whether to
run this on a machine they care about, so it follows the data from the node to the person
looking at the page, and at each step it says what crosses and how I know.

I have not checked any of this against a live node. What I call tested I ran on 29-09-2026 on
a development machine, against the code at commit `ddd9a80`, with events and addresses that I
made up, and what I call read I found in the code and did not run.

```
darkfid --1--> collector --2--> disk
                   |
                   3
                   v
                server --4--> panel --5--> whoever is looking
```

## 1. darkfid → collector

I only read this part, because there was no node on the machine where I tested. The scripts
connect to `127.0.0.1`, which is written into them so that only the port can be configured.
They subscribe to the debug feed with `dnet.subscribe_events`, they ask for the block height,
and they call `dnet.switch`, which turns the debug feed on when the recorder starts and off
again when it exits. Apart from that the collector reads the cgroup of the node, its journal
and the socket table of the kernel, and it does not open the data directory of the node, its
config or its wallet.

Every unit runs as root, because none of them sets `User=`. The one piece that acts on the
node is `darkfid-memguard`, which restarts darkfid, and it is optional and `install.sh` does
not install it. So what the collector does to the node is read from it and flip the switch
of the debug feed.

## 2. collector → disk

Everything is written under `/var/log/darknode`, and for me this is the most sensitive part
of the system, because the raw feed is stored there with the real addresses of your peers.

| File | Peer addresses | Expires |
|---|---|---|
| `dnet-*.jsonl` | yes | after `HISTORY_MAX_DAYS`, 120 by default |
| `pulse-ids.json` | yes | never |
| `pulse-*.jsonl` | no, only pseudonyms | never |
| `snapshots-*.jsonl` | no | after `HISTORY_MAX_DAYS` |
| `blocks.tsv` | no | never |

`dnet-*.jsonl` has every message the node sent or received, with the address of the peer and
the time to the millisecond, so whoever can read it knows who the node was talking to during
the last four months. `pulse-ids.json` only exists if you publish, and it is the map from
each address to its pseudonym, so together with a published feed it gives the addresses back.

I have not looked at a live install, but from the unit files the directory and the files are
created with the defaults of systemd, which would make them readable by every local user, and
`ls -l /var/log/darknode` will tell you whether that is the case on your machine. Neither
file is encrypted, and if `/var/log` goes into your backups then the addresses go with it. So
before installing I think it is worth knowing who else can read that directory and where its
copies end up.

## 3. collector → server

This is the only boundary that crosses the network, and with one exception that I describe
at the end (item 3), a default install sends nothing across it. I read `install.sh` rather
than running it, and without `--publish` it does not install `node-pulse`, which is the part
that sends the event feed, and with `--publish` it installs it stopped, so that you can read
what it sends before you start it.

Once the URLs are configured, `node-pulse.py` sends the events about once a second, which
for each message is the time, a pseudonym, the direction and the name of the command, and
`darknode-export.sh` sends a snapshot once a minute with height, tip, peer count, memory,
temperature, uptime, the mining figures and up to twelve lines of contract log. There is
also a daily digest of aggregates, with its timer installed but not enabled.

I only tested this with made-up events, and with those no address left the machine. I gave
`node-pulse.py` 42 events from seven peers, with IPv4, IPv6, onion and hostname addresses,
and no part of any address was in what the server received or in the local copy of what was
sent.

There are other things that do leave. The pseudonyms are stable, so `p3` is the same peer
next month, and the names you give to your own machines in `PEER_NODE_MAP` are sent although
their addresses are not. The contract log lines are free text that goes through a regex
which removes anything with the shape of a wallet address, and I have not shown that nothing
else identifying can appear in them, so if that worries you `WASM_TAIL_N=0` turns them off.
The POST is an ordinary request from your machine, so the server and whoever hosts it see
your IP address. And the collector accepts an `http://` URL, in which case the token and the
feed travel in the clear.

You can look at what would be sent before anything is sent:

```bash
DRY_RUN=1 /usr/local/bin/darknode-export.sh | jq .       # the snapshot
darknode-digest.py --dry-run --out /tmp/digest.json      # the digest
# node-pulse with PULSE_URL unset sends nothing and still writes pulse-*.jsonl
```

A published feed is a deanonymization surface even when there is no address in it, and that
is written up in [issue #1](https://github.com/reymom/darkscope/issues/1), so I do not repeat
it here.

## 4. server → panel

I tested the server by running it on my machine and pushing to it. It keeps five minutes of
events and the last snapshot of each machine in memory, and of the snapshot it serves only
height, tip, peer count, uptime and memory, so the contract log lines are not served to
anybody. From reading the code it writes nothing to disk and keeps no access log.

Writing needs `DARKSCOPE_TOKEN`, and a push without it got a 401, but if the variable is
unset then anyone who can reach the port can push, and the server only prints a warning when
it starts. Reading is open unless `DARKSCOPE_READ_TOKEN` is set, and the server listens on
every interface, so on a home network a local install can be read by the whole network.

The server trusts its collectors, which means that it checks the token and the name of the
machine and it does not check that the events are real, so whoever holds the token can push
any events for any machine in `machines.json` and the graph will draw them. That gives them a
graph that lies and it does not give them a node, because the server has no connection to any
node and no code that could open one. All the collectors share one token, so if it leaks it
has to be changed on every machine.

The server is the `http.server` of Python, with no TLS, no rate limit and no limit on the
size of a request, so if it faces the internet I would put it behind a reverse proxy.

## 5. panel → whoever is looking

The panel is static files and three GET requests to the server it was loaded from. I read
the source and the built bundle, and it sends nothing, loads nothing from another site, sets
no cookie and stores nothing in the browser. What the viewer sees is the names and
descriptions in `machines.json`, the height, memory, peer count and uptime of each machine,
and every message with its pseudonym and its time, so I would not put anything in
`machines.json` that I would not publish.

The question everybody asks is whether the panel can attack the collector, and it cannot,
because there is no path back. The panel only reads from the server, the server has no code
that connects to a collector or to a node, and the collector listens on no port and only
makes outgoing requests, where the most it does with the reply is log it. The part about
listening I tested for `node-pulse` and read for the other scripts. So nothing that a viewer
does in the page reaches the machine the node runs on.

The case to keep in mind is the server running on the same machine as the node, because then
a bug in the server is a bug on that machine. The repo has no unit file for the server yet,
so for now I would run it as a user that owns nothing.

## Where the code does not match this yet

These are the places where the code at `ddd9a80` does less than what is written above. I
found them while checking, and none of them is fixed.

1. The server does not check peer names. A comment in `node-pulse.py` says that the route
   which receives the events rejects anything outside `[a-z0-9-]`, and `server.py` has no
   such check, so when I pushed a peer name with the shape of an address it was served back.
   This means that the addresses staying on the machine depends on one function in the
   collector, with nothing behind it.
2. The panel runs HTML that arrives in a snapshot. I pushed a snapshot where the peer count
   was an `<img>` tag with an `onerror` handler, and the script ran in headless Chrome, so
   today whoever holds the token can also run script in the browser of whoever is looking,
   although that script still cannot reach a node.
3. A default install tries to reach a placeholder. The example config that `install.sh`
   copies sets `INGEST_URL` to a name under `.example`, and the exporter tries it once a
   minute. The name cannot resolve, so no data is delivered, but each attempt is a DNS lookup
   that leaves the machine.
4. `DARKSCOPE_READ_TOKEN` locks out the panel that ships with the server, because the panel
   does not send the token, so with it set the API answered 401 and the page stays empty
   unless a proxy adds the header.
5. The check on static files has a hole. A request for `/../dist-old/x` was served when a
   directory whose name starts with `dist` was sitting next to `panel/dist`, and paths
   further out got a 403.
6. `pulse-*.jsonl`, `pulse-ids.json` and `blocks.tsv` are never expired.

## What I have not tested

Nothing here was tested against a live node. I have not run `install.sh` from start to
finish, and from reading it I think that today it stops at the panel step, because it still
copies `panel/serve.py`, `panel/app.js` and `panel/darkscope-panel.service`, which are no
longer in the repo. I have also not checked the file permissions on a real install, not run
the digest (I read that it drops the addresses), and not given the collector a malformed or
hostile feed.
