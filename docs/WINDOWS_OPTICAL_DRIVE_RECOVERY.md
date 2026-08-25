# Windows optical-drive mapping and recovery

This note records the Windows drive-mapping behavior added on 2026-08-10 and
the safe recovery procedure for an optical drive that stops reporting its
loaded disc.

## Stable drive mapping

- MakeMKV drive numbers are temporary command slots. RipWeaver binds each slot
  to a hashed Windows Plug-and-Play identity.
- The setup wizard reviews the complete currently detected device set. It can
  mark every detected drive as usable at once or retain per-device `Use` and
  `Ignore` choices.
- A newly detected or changed hardware identity is always unmapped. It does not
  inherit an older drive's approval, even when the sanitized model description
  is identical.
- A safe descriptor match can warn that a USB device may have been
  re-enumerated. Saving the exact wizard snapshot retires absent older trusted
  identities so they cannot silently regain approval later.
- All five currently installed optical drives are intentional ripping devices:
  three SATA drives and two generic USB optical devices.
- A Windows-only provisional slot cannot prepare or rip a disc until a
  read-only MakeMKV refresh confirms the same current slot.
- If automatic processing is enabled, the wizard has a separate explicit
  option to continue loaded discs after saving. Clearing it changes only the
  private map and starts no disc work.

The private map stores only identity hashes, sanitized model/connection
descriptors, and trusted, ignored, or retired states. It does not store raw
Plug-and-Play IDs, serial numbers, or drive letters.

## Loaded, unreadable, and empty are different states

Windows may detect an optical device and even obtain a volume label while File
Explorer still reports that it cannot read the disc. That does not prove the
tray is empty, and it does not prove MakeMKV cannot read the disc directly.

Conversely, a wedged drive can remain present with Windows device status `OK`
while reporting `MediaLoaded: False` for a disc that is physically in the tray.
During the 2026-08-10 incident:

- Windows still enumerated the affected internal SATA Blu-ray drive;
- its device status was `OK`, but its media-loaded state was false;
- File Explorer could not read the disc and software eject failed; and
- no MakeMKV process was running.

This combination supports a stale driver/device state or locked drive firmware,
not an active MakeMKV lock. Do not repeatedly open the drive in File Explorer;
close Explorer errors before retrying MakeMKV discovery.

Future UI work should represent at least `empty`, `loaded`, and
`loaded/unreadable or unknown` separately. A failed Windows volume mount must
not be treated as proof of an empty tray. MakeMKV remains authoritative before
disc preparation or ripping.

## Safe recovery order

Before resetting a device, confirm that no RipWeaver rip and no MakeMKV GUI or
CLI process is reading any optical drive.

### 1. Restart one exact Windows device

Open PowerShell as Administrator. Replace the placeholder drive letter and
expected model text, then review the metadata before issuing the restart:

```powershell
$optical = Get-CimInstance Win32_CDROMDrive -Filter "Drive = '<LETTER>:'"
$optical | Select-Object Drive, Name, MediaLoaded, Status

if ($optical.Name -notlike '*<EXPECTED MODEL TEXT>*') {
    throw 'The drive letter is not bound to the expected optical device.'
}

pnputil /restart-device "$($optical.PNPDeviceID)"
pnputil /scan-devices
```

Never use a broad `CDROM` class restart on a multi-drive system: it can reset
all optical drives, including unaffected active pipelines. Do not use
`pnputil /remove-device` for this recovery.

The equivalent graphical operation is Device Manager, `DVD/CD-ROM drives`, the
exact reviewed model, `Disable device`, wait ten seconds, then `Enable device`.

### 2. Perform a real hardware power cycle

If the targeted Windows restart does not recover the drive:

- For a USB drive, disconnect both USB and external power, wait 30 seconds,
  reconnect power, and then reconnect USB. Prefer the same USB port. A changed
  identity or port remains blocked until the mapping wizard is reviewed.
- For an internal SATA drive, shut Windows down completely instead of choosing
  Restart, switch off or unplug the PC for 30 seconds, and then power it on.

After recovery, do not test by repeatedly opening the disc in File Explorer.
Close the tray, wait 10 to 15 seconds, and use RipWeaver's read-only drive
refresh.

Use a tray's emergency-eject pinhole only as a final physical recovery with the
computer powered off and disconnected. It is not a software reset.

## Tray-control follow-up

Windows provides device-specific eject and load-media controls, but File
Explorer normally exposes only eject. A future RipWeaver `Close tray and
refresh` action should:

- target the exact trusted hashed device mapping;
- refuse an unmapped, ignored, or changed identity;
- refuse active/queued rip work that makes tray control unsafe;
- require explicit tray-control confirmation; and
- request a read-only MakeMKV refresh only after the close operation settles.

Generic CD-audio commands are unsuitable on a five-drive system because they
can address the wrong tray.

## Safety record

The incident diagnosis used only path-redacted Windows device metadata and a
process-list check. No restart, disable, eject, load, MakeMKV command, disc read,
rip, transcode, rename, move, delete, or media-library change was performed by
Codex while diagnosing it.
