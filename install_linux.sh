#!/bin/bash

current_dir=`dirname "${BASH_SOURCE[0]}"`
SCRIPT_DIR="$( cd "$( dirname "$0" )" && pwd )/scripts"
pyexec=`"$SCRIPT_DIR"/python.sh`

"$pyexec" "$current_dir/kalite/manage.py" install

initd_available=`command -v update-rc.d`
if [ $initd_available ]; then
    while true
    do
        echo
        echo "Do you wish to set the KA Lite server to run in the background automatically"
        echo -n "when you start this computer (you will need root/sudo privileges) [Y/N]? "
        read CONFIRM
        case $CONFIRM in
            y|Y)
                echo
                sudo "$SCRIPT_DIR/runatboot.sh"
                echo
                break
                ;;
            n|N)
                echo
                break
                ;;
        esac
    done
fi
