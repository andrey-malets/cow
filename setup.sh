#!/bin/bash

install_prereqs() {
    packages=(nginx kpartx iscsitarget iscsitarget-dkms)
    apt-get -y install "${packages[@]}"
}

setup_nginx() {
    HOSTNAME=$(hostname -f)
    CONFIG="$(pwd)/conf/$HOSTNAME.sh"
    TARGET=/etc/nginx/sites-available/cow

    if [[ ! -r "$CONFIG" ]]; then
        echo "cannot read $CONFIG" 1>&2
        exit 1
    else
        . "$CONFIG"
    fi

    opts=(WEB_PATH TARGET_HOST)
    for opt in "${opts[@]}"; do
        if [[ -z "${!opt}" ]]; then
            echo "$opt must be configured" 1>&2
            exit 1
        fi
    done

    cat > "$TARGET" <<END
server {
    server_name $TARGET_HOST;
    root        $WEB_PATH;
}
END

    mkdir -p "$WEB_PATH"
    touch "$WEB_PATH/index.html"

    ln -sf "$TARGET" /etc/nginx/sites-enabled
    /etc/init.d/nginx restart
}

install_prereqs
setup_nginx
