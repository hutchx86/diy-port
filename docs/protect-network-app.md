# Protect, the Network app, and diy-port

Why Protect keeps logging `Failed to get device details from Network application for <mac>: 404`
for diy-port, and what the "Internal Storage" panel on a real AI Port depends on. Read live on
a UCG Max (Protect 7.3.74, Network on PostgreSQL) on 2026-11-19, by comparing diy-port with a
real AI Port (firmware 5.1.12) adopted that day. Module numbers are from the extracted service
bundle (`service-expanded/modules`).

## The error

`networkAppMiddleware` (module 727717, `deviceInfo`) runs
`GET http://127.0.0.1:8081/v2/api/devices/<mac>/details` on the Network app. Two callers use
it:

- `devices.wifiConnectionState.refreshConnectionState` (620729): every connected device of
  every model, about once a minute;
- `cameras.uplinkDevice.onCameraConnect` (408614): once per camera connect.

Any non-2xx answer is logged as the error above and returns `null`. The only effect is that
Protect stores no `uplinkDevice` or `wifiConnectionState` for the device. Adoption, streams and
smart detections are not affected.

Network's 404 body is `{"code":"api.err.UnifiDeviceNotFound","errorCode":404}`.

## What Network needs to answer

For the real AI Port the same call returns 200:

```json
{"is_wired": true, "remote_port_number": 12,
 "uplink_device": {"mac": "<switch mac>", "name": "USW Flex Mini", "uri": "/network/default/devices/properties/<switch mac>"}}
```

Network answers from a **client** record, not from its own device table. A real AI Port is
not in `config.device`, which holds only the switches, APs and gateway Network adopted itself.
Two things must both be true:

1. **Network has seen the MAC on the wire.** It keeps a client row in `config.user_client`
   (database `unifi-network`, Postgres :5434) for every MAC it has seen, with `is_wired`, the
   switch port, and `first_seen`/`last_seen`.
2. **unifi-core has filled in that row's `unifi_device_info_from_ucore`.** When Protect adopts
   a device, it is registered with unifi-core (database `unifi-core`, Postgres :5432, table
   `devices`). Network then copies that device's info onto the client row with the same MAC:
   name, `product_line: PROTECT`, model, firmware, `device_state`, icons. The same info makes
   the device appear in Network's device list as a UniFi device managed by Protect.

Timeline for the real AI Port:

| Time (UTC) | Event |
|---|---|
| 22:08:52 | Client row created: Network sees its MAC on the switch |
| 22:21:47 | Protect adopts it, and it is registered in unifi-core |
| 22:22:11, 22:32:10 | Two 404s, before the ucore info reached the client row |
| 22:32:13 | Client row updated (ucore info attached); every call after this is 200 |

## Why diy-port gets a 404

| | Real AI Port | diy-port | ONVIF camera |
|---|---|---|---|
| Registered in unifi-core | yes | yes | yes |
| Network client row for the MAC | yes | **no** | yes |
| `unifi_device_info_from_ucore` | filled in | no row to fill | empty |
| `/devices/<mac>/details` | 200 | 404 | 404 |

diy-port claims an AI Port MAC (in the Ubiquiti `FC:EC:DA` range or locally administered) that
no network card ever sends from. The host's own NIC carries diy-port's traffic. In the console's
ARP table, a diy-port's IP resolves to the host's MAC, not to the MAC it adopted under. Network
therefore never creates a client row for the claimed MAC, so the ucore info has nowhere to go.
The details call 404s forever, and diy-port never appears in Network's device list.

Third-party ONVIF cameras always 404 as well, even when paired with a real AI Port. Network
sees them, but unifi-core lists them without UniFi device info. diy-port can't change that.

## Fixing it

Nothing in the AI Port protocol changes this; Network has to see the claimed MAC on the wire.

- **Linux / Docker**: run the container on a macvlan network with `mac_address` set to the
  MAC diy-port adopts under. Its traffic then comes from that MAC, and Network makes a client
  row for it.
- **macOS**: a USB Ethernet adapter whose MAC is set to the claimed MAC
  (`sudo ifconfig enX ether <mac>`), with the instance's `DIYPORT_BIND_IP` on that adapter.
  Several instances on one Mac would each need their own adapter.
- **Use the host NIC's MAC as diy-port's MAC.** This needs no extra hardware, but only one
  instance can use each NIC, and Network will label the host itself as the AI Port.

Before trying any of these, it is worth checking whether Protect cares about a MAC outside the
Ubiquiti range. Today's locally administered `02:…` instance adopts fine, so it probably
doesn't.

Leaving it alone is also reasonable: the cost is one error line per device per minute.

## The Internal Storage panel

A real AI Port's device panel in Protect shows **Internal Storage: "Insert an SD card to
activate Internal Storage for recordings backup."** (`DEVICE_SIDE_PANEL_INTERNAL_STORAGE_INFO`,
in the `vantage.js` UI bundle). diy-port shows no such panel.

The panel renders only when the device's `featureFlags.hasEdgeRecording` is true and the device
is available. That flag is parsed from the hello's `features.edgeRecording` (module 546416:
`hasEdgeRecording: Boolean(t.edgeRecording)`). The empty-slot message shows when
`featureFlags.hotplug.sdCardAttached` is false. Protect builds `hotplug`, with `sdCardAttached:
false`, whenever the hello's `features` has a `hotplug` object. For an AI Port, the panel adds
a `deviceMac` picker, which suggests the SD card backs up recordings per paired camera.

What the Protect database holds (`aiports` table):

| | `featureFlags.hasEdgeRecording` | `featureFlags.hotplug` |
|---|---|---|
| Real AI Port | `true` | `{standaloneAdoption: false, sdCardAttached: false, extender: {...}}` |
| diy-port | `false` | `null` |

diy-port's `ubnt_avclient_hello` (`avclient.py`) sends neither `edgeRecording` nor `hotplug`.
Adding them would make the panel appear. Behind it, though, Protect drives edge recording:
`updateDeviceSDCardMountState`, `stats.sdCard`/`storageSlots`, `syncEdgeRecordingState` and
external-storage settings. Supporting it means implementing local recording backup, not
setting a flag.

## Commands used

```bash
# Network's answer for a MAC (on the console)
curl -s http://127.0.0.1:8081/v2/api/devices/<mac>/details
# Client row and its ucore info
psql -U postgres -p 5434 -d unifi-network -c "select mac,is_wired,unifi_device_info_from_ucore from config.user_client where mac='<mac>'"
# unifi-core's device registry
psql -U postgres -p 5432 -d unifi-core -c "select mac,adopted_at,device_id from devices"
# Which MAC the console sees at an IP
ip neigh show <ip>
# Protect's view of the AI Ports
psql -U postgres -p 5433 -d unifi-protect -c "select mac,host,\"featureFlags\"->'hasEdgeRecording' from aiports"
```
