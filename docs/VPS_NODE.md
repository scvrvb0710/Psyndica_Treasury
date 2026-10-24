# Interim node: pruned Bitcoin Knots on a VPS

A stopgap until you buy node hardware. The node is a **data source only**:
no wallet, no keys, no funds. A VPS is someone else's computer.

## 1. Server
- 2 vCPU, 4 GB RAM, 80 GB+ SSD, Ubuntu/Debian LTS.
- Enough monthly traffic for the initial sync (700+ GB inbound).
- Read the provider's terms: most ban mining, but nodes are usually fine.

## 2. Harden before installing anything
```bash
adduser --disabled-password bitcoin
# SSH: keys only (PasswordAuthentication no, PermitRootLogin no in sshd_config)
ufw default deny incoming
ufw allow OpenSSH
ufw allow 8333/tcp        # optional: accept inbound peers
ufw enable
```
Never open 8332 (RPC).

## 3. Install Knots and verify it
Download the release tarball, `SHA256SUMS`, and `SHA256SUMS.asc` from
bitcoinknots.org. Import the signer's key from a source you trust, then:
```bash
gpg --verify SHA256SUMS.asc SHA256SUMS
sha256sum --ignore-missing --check SHA256SUMS
```
Stop if either check fails.

## 4. `~bitcoin/.bitcoin/bitcoin.conf`
```
prune=20000            # keep ~20 GB of recent blocks
dbcache=2000           # faster initial sync; drop to 450 once synced
disablewallet=1        # no keys on this box
server=1
rpcbind=127.0.0.1
rpcallowip=127.0.0.1
maxuploadtarget=5000   # MiB/day cap so traffic bills don't spike
```
Run `bitcoind` under systemd as the `bitcoin` user (Knots ships an example
unit in `contrib/init/`). The initial sync takes several days on a small VPS.

## 5. Scanner (shadow mode, on the same box)
Run it as the `bitcoin` user so it can read the node's `.cookie`:
```bash
sudo -u bitcoin python3 -m sats_engine scan --rpc            # one reading
sudo -u bitcoin python3 -m sats_engine scan --rpc --watch \
    --save /home/bitcoin/snaps.csv >> /home/bitcoin/scan.jsonl
```
While the node is syncing, every reading is empty and the scanner warns
`syncing NN%`. That is correct: mempool data means nothing until the node
has caught up.

Example `/etc/systemd/system/sats-scan.service`:
```ini
[Unit]
Description=Sats Ratchet on-chain scanner (shadow mode)
After=bitcoind.service
Requires=bitcoind.service

[Service]
User=bitcoin
WorkingDirectory=/home/bitcoin/Psyndica_Treasury
ExecStart=/usr/bin/python3 -m sats_engine scan --rpc --watch --save /home/bitcoin/snaps.csv
StandardOutput=append:/home/bitcoin/scan.jsonl
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

## 6. Viewing from home
Use `ssh -L 8332:127.0.0.1:8332 bitcoin@vps` or Tailscale. Don't expose
anything publicly.

## Notes
- Don't mix `--rpc` history with mempool.space history in one backtest: the
  whale reading is measured differently.
- When you move to your own hardware, copy `snaps.csv` over and keep going.
