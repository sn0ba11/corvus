# Changelog

## 0.2.0-rc14

- The Windows installer upgrades installations made from Corvus builds that predate the public release. It previously stopped with a rejected (non-fast-forward) Git fetch. The installer moves the application checkout to the release and keeps the earlier version in a `corvus-pre-migration-*` Git branch. Local configuration and Docker volumes are unchanged.

## 0.2.0-rc13

- The Windows installer, its shortcuts and the uninstaller carry the Corvus raven icon at every standard size from 16 to 256 pixels. Windows and Linux builds embed identical artwork.
- The installation and uninstallation wizards show Corvus artwork in place of the stock NSIS images.
- The installer metadata names Corvus as product and publisher and records the release version.

## 0.2.0-rc12

- Initial public release candidate of Corvus.
- Includes local investigations, evidence review and export, and optional external collectors.
