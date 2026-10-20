# TEST-UNIVERSE

## Ubuntu VM sessions

This repository runs a private session manager on port **6080**. Each account
gets its own persistent Ubuntu 26.04 QEMU/KVM virtual machine and host file
drop folder. Admin-created accounts only; public signup is disabled.

### Start and create the first administrator

Run `./start-vm.sh`. The first run downloads and verifies the official Ubuntu
26.04.1 desktop ISO (about 6.5 GB), builds the manager and Guacamole, and starts
them. The existing administrator and Lubuntu disk are preserved. Open port
**6080** in VS Code's Ports view or open `vm.html`. Keep the forwarded port private.
Port 6080 serves the sign-in page and Apache Guacamole console through one
gateway. For a local Linux host, `vm.html` connects to
`http://127.0.0.1:6080`.

Sign in as administrator and create user accounts in the admin panel. Users
sign in and start their own desktop; each account's first boot enters the
Ubuntu installer. Install Ubuntu to the new Ubuntu virtual disk. After the first
desktop login, open a terminal and run:

```sh
sudo mkdir -p /mnt/host-setup
sudo mount -t 9p -o trans=virtio,version=9p2000.L,ro hostsetup /mnt/host-setup
sudo /mnt/host-setup/guest-setup.sh
```

The guest setup mounts the host drop/control folders, installs the latest GA
Java runtime, creates an SKlauncher
desktop shortcut, imports files from the account's host folder into
`~/Downloads` (including when new files are added while the guest is paused),
pauses the VM when the GNOME session logs out, and records that Ubuntu is
installed so later manager restarts boot from the virtual disk. Put files under
`drop/<username>/` on the host; each guest sees only its own folder. New files
are copied into Downloads while the guest is running, including shortly after
resuming a paused desktop.
Put SKlauncher's JAR there as well. Minecraft versions may require a different
Java version, selectable in SKlauncher's version settings.

### Session behavior

- Each user has one isolated VM and persistent virtual disk.
- A running session pauses after 60 minutes; resuming starts a new 60-minute
  interval.
- Closing all Guacamole console tabs pauses the VM after a 60-second heartbeat grace
  period. Signing out in the web manager pauses it immediately. Logging out
  of the Ubuntu desktop also pauses it.
- Paused sessions retain VM memory and state until resumed. A manager/container
  restart preserves the virtual disk but loses in-memory desktop state. At
  most two VMs (including paused VMs) are kept active.
- Apache Guacamole provides the browser desktop console, fullscreen, keyboard,
  and mouse support. Browser and host policies may limit pointer lock and
  latency; this is not a native gaming setup.

Resource ceilings are enforced: at most two VMs, two vCPUs and 4 GB RAM per
VM, a 32 GB sparse disk per account, and one hour per running interval. The
manager container is capped at four CPUs and 10 GB RAM. No GPU device is
passed through and no cloud GPU integration is configured, so this setup
cannot consume GPU hours or incur GPU charges.

### Performance and limitations

QEMU uses KVM, host CPU features, VirtIO disk, and an emulated e1000 network
adapter for broad installer driver support. The manager and guacd use the
Codespace host's DNS/network route but bind only to the private Docker bridge.
The browser gateway listens on loopback port 6080; it is the only service to
forward in VS Code. This addresses the installer DNS/connectivity problem
without publishing VNC or the Guacamole backend.
Virtual disks grow as needed. This Codespace has no GPU passthrough; browser
remote desktop adds input latency and does not stream audio, so this is not
equivalent to native gaming hardware.

Run `docker compose down` to stop the manager and all VMs. Account disks and
the database remain under `vm-data/`; host inboxes are under `drop/`. Ubuntu
uses `ubuntu-26.04.qcow2`; the previous `lubuntu.qcow2` disk is left untouched.