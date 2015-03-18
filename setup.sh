#!/bin/bash

install_prereqs() {
    packages=(nginx kpartx iscsitarget iscsitarget-dkms)
    apt-get -y install "${packages[@]}"
}

load_config() {
    local config="$(dirname "$0")/conf/$(hostname -f).sh"

    if [[ ! -r "$config" ]]; then
        echo "cannot read $config" 1>&2
        exit 1
    else
        . "$config"
    fi

    opts=(WEB_PATH TARGET_HOST ISCSI_TARGET_PORT)
    for opt in "${opts[@]}"; do
        if [[ -z "${!opt}" ]]; then
            echo "$opt must be configured" 1>&2
            exit 1
        fi
    done

}

setup_nginx() {
    local target=/etc/nginx/sites-available/cow

    cat > "$target" <<END
server {
    server_name  $TARGET_HOST;
    root         $WEB_PATH;
    default_type text/plain;
}
END

    mkdir -p "$WEB_PATH"
    touch "$WEB_PATH/index.html"

    ln -sf "$target" /etc/nginx/sites-enabled
    /etc/init.d/nginx restart
}

setup_ietd() {
    local target=/etc/default/iscsitarget
    cat > "$target" <<END
ISCSITARGET_ENABLE=true
ISCSITARGET_OPTIONS="-p $ISCSI_TARGET_PORT"
END
    for i in {1..3}; do
        /etc/init.d/iscsitarget restart && break
    done
}

load_config

install_prereqs
setup_nginx
setup_ietd
