# M710q Infineon TPM 1.2 to 2.0 conversion

> This runbook does not apply to the selected HP ProDesk 600 G4 DM cp2
> candidate, which already exposes a usable native TPM 2.0. It remains only for
> separately inventoried Lenovo M710q candidates matching every gate below.

This is a deliberately narrow, console-attended runbook for a Lenovo
ThinkCentre M710q whose Infineon TPM reports specification `1.2` and firmware
`6.43`. The only admitted sequence is:

```text
TPM 1.2 6.43 -> TPM 2.0 7.62.3126.0 -> TPM 2.0 7.85.4555.0
```

Do not skip the intermediate version, reverse the order, or apply this
runbook to a different model, TPM vendor, or starting firmware.

## Non-negotiable safety boundary

- Treat the conversion as destructive to TPM ownership, TPM-resident keys,
  and data sealed to the old TPM state. Back up any required recovery keys and
  assume the old trust state will not survive.
- Close every TPM-using application. Fully decrypt every TPM-backed volume
  and turn BitLocker off; merely suspending protection is not the same gate.
  `manage-bde.exe -status` must show the volumes as fully decrypted with
  protection off before either firmware stage.
- Use stable AC power. Once a stage has accepted the capsule and begun its
  shutdown, beeps, or reboots, do not remove power, press reset, use a smart
  plug, or interrupt it. Lenovo says to expect several beeps and several
  reboots while the capsule is written.
- Run from an Administrator Command Prompt in 64-bit Windows. Do not use the
  silent option: the attended prompts are part of this ceremony.
- Never put a BIOS Administrator password on a command line or in a wrapper,
  log, screenshot, or this repository. Supply it interactively if the machine
  has one configured. Stop if the prompt or machine state is unexpected.

This runbook does not invoke a separate TPM clear, initialization, or
ownership command. The firmware conversion itself may erase or invalidate the
existing TPM trust state as warned above. The verification commands below are
queries only.

## Hardware and package gates

Before staging Windows, compare the machine with trusted inventory and its
physical label. It must be an M710q with an Infineon TPM and BIOS
`M1AKT30A` or newer. Lenovo's newer bundle explicitly lists the M710q and that
minimum BIOS. The observed fabric candidate uses `M1AKT40A`, which satisfies
that minimum.

The retained official bundles are:

- Stage 1:
  [DS501960](https://support.lenovo.com/us/en/downloads/ds501960-tpm-fw-switch-tool-for-windows-thinkcentre-and-thinkstation-systems),
  locally retained as `tpmfwcapupd-ds501960.zip`.
  - Archive SHA-256:
    `46c29aec47386b03d060cf6068f5e23ca00a0b06710fb1ef7ae8680f6c29363b`
  - `IFX_Tpm1220DevCap.BIN` SHA-256:
    `04213fae23e0e813f29ab52d8a413b6ae8ee605f7461e54ba25a6f5e2323a424`
- Stage 2:
  [DS557757](https://support.lenovo.com/us/en/downloads/ds557757-tpm-firmware-update-tool-for-windows-11-64-bit-10-64-bit-thinkcentre-and-thinkstation-systems),
  locally retained as `tpmfwcapupd_m910_tpm20_7.85.4555.0.zip`.
  - Archive SHA-256:
    `fea68e3652671910d85bfe3cce00b9196d56efac9197e68b7b71c8854e06d341`
  - `IFX_Tpm2020DevCap.BIN` SHA-256:
    `07f25cc19fc34b6f19e22de24d2fa6df50d3fe353dce6e60e2dddeb9adba625b`

The archive SHA-256 values above are Lenovo-published checksums and match the
locally retained archives. The selected capsule SHA-256 values are locally
computed admission pins. When reacquiring either bundle, use only the exact
Lenovo-owned DS501960 or DS557757 page above, verify Lenovo's published archive
digest before extraction, and then verify the selected capsule digest. Treat
any vendor package or checksum change as a new review; a matching filename is
not sufficient. Lenovo solution
[HT506395](https://support.lenovo.com/ee/en/solutions/ht506395) directs the
affected ThinkCentre TPM-version-switch workflow to DS501960.

The DS501960 conversion capsule itself contains the target version string
`7.62.3126.0`. The DS557757 readme says its TPM 2.0 update is valid **only**
from `TPM20_7.62.3126.0` to `TPM20_7.85.4555.0`. Its readme also supplies the
M710q BIOS prerequisite above. Reconfirm Lenovo's DS501960 product selection
before using this procedure on any M710q other than the inventoried candidate;
the retained DS501960 readme does not itself enumerate supported models.

Verify the archive digests before extraction and the two selected capsule
digests after extraction. Stop on any mismatch. Do not substitute a similarly
named package or capsule. In Windows, use `certutil -hashfile FILE SHA256` from
the same Administrator Command Prompt.

## Query-only baseline

From an Administrator Command Prompt, capture:

```bat
wmic computersystem get manufacturer,model
wmic csproduct get name,identifyingnumber,uuid
wmic bios get smbiosbiosversion
wmic /namespace:\\root\cimv2\security\microsofttpm path win32_tpm get /value
manage-bde.exe -status
```

The admitted starting state is Lenovo M710q, BIOS `M1AKT30A` or newer,
Infineon (`IFX`) TPM specification `1.2`, and manufacturer firmware `6.43`,
with no encrypted or protected BitLocker volume. Stop if any field differs.
Do not run `Clear-Tpm`, `Initialize-Tpm`, or a BitLocker-changing command as a
substitute for meeting this gate.

## Stage 1: convert 6.43 to TPM 2.0 7.62.3126.0

Open an Administrator Command Prompt in the extracted DS501960
`TPMFWCAPUPD` directory and run:

```bat
flash.cmd /2
```

Expected attended flow:

1. The tool reports BitLocker off. If it reports BitLocker on, answer `N`,
   decrypt the volume, and restart the whole gate.
2. It reports that it will switch the TPM specification from 1.2 to 2.0 and
   asks `Do you want to continue to switch TPM FW? Y/N:`. Answer `Y` only
   after the identity, version, digest, encryption, and power gates pass.
3. It prompts for the BIOS setup Administrator password. Enter it
   interactively if configured; never pass it as an argument.
4. A successful submission announces a shutdown/reboot. The machine then may
   beep and reboot several times. Leave it completely alone until Windows is
   stably back at the login or desktop.

Treat `not supported`, `latest`, any update error, or a return to the prompt
without the announced shutdown as a stop condition. Do not retry blindly or
jump to Stage 2.

Run the query-only baseline again. Require TPM specification `2.0` and
firmware `7.62.3126.0` before continuing. If Windows reports only a shortened
manufacturer version, retain the full query output and independently confirm
the complete version; do not infer success merely because the machine boots.

## Stage 2: update 7.62.3126.0 to 7.85.4555.0

Only after Stage 1's exact result is proven, open an Administrator Command
Prompt in the extracted DS557757
`TPMFWCAPUPD_M910_TPM20_7.85.4555.0` directory and run:

```bat
flash.cmd /2
```

The tool should identify this as a TPM 2.0 firmware update, may prompt for the
BIOS setup Administrator password, and on successful submission will shut
down/reboot. Again, expect beeps and multiple reboots and do not interrupt
power until Windows has returned and remained stable.

Run the query-only baseline a final time. Require Infineon TPM specification
`2.0` and firmware `7.85.4555.0`. Preserve the before, intermediate, and after
query output with the candidate's private commissioning record. A successful
firmware query is not authorization to initialize, clear, enroll, or take
ownership of the TPM; those are separate admission steps.

## Git and media boundary

Only this runbook belongs in Git. Windows installation media, Lenovo ZIPs,
extracted `.exe`, `.sys`, `.bin`, and `flash.cmd` files, local operator
wrappers, generated USB/disk/ISO images, TPM logs, BIOS passwords, BitLocker
recovery material, and candidate-specific evidence must remain on trusted
storage outside the checkout. Generated temporary Windows media must not
contain Git credentials, cluster secrets, or an unattended answer file.
