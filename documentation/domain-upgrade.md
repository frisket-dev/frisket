# Upcoming project storage upgrade

This upgrade changes how Frisket records edits, action results, and their history.
Existing projects are upgraded automatically when opened. Before upgrading, close
Frisket and copy each complete `.frisket` project folder, including its database
and files. Keep that backup if you might need to return to an older release.

Your imported data, files, saved results, and manual edits remain. The upgrade
starts a **new Undo history** for existing projects: you cannot Undo or Redo
changes made before the upgrade. Retained results and provenance are separate
from Undo history.

Unfinished work from the old execution system is stopped. Run that work again
after upgrading. Existing derived sheets remain, but their old refresh setups
must be recreated before they can refresh. Ordinary source polling is retained.

Updated Frisket Cloud requires the matching Desktop execution protocol. If an
older Desktop client tries to start Cloud work, it receives an update-required
message before a provider request is made. Upgrade Desktop to continue.

Do not open an upgraded project with an older Frisket release. To return to an
older release, restore the complete pre-upgrade backup instead. Hosted operators
must stop writers and back up both the control databases and project storage
together before activation; changing only the application image is not a
database rollback.
