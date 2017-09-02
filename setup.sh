#!/bin/bash

set -e

config_name() {
    local base_name=$(basename "$1")
    echo "${base_name%%.sh}"
}

install_prereqs() {
    local packages=(nginx kpartx iscsitarget iscsitarget-dkms)
    apt-get -y install "${packages[@]}"
}

load_host_config() {
    local config=$1

    if [[ ! -r "$config" ]]; then
        echo "cannot read host config $config" 1>&2
        exit 1
    else
        . "$config"
    fi

    local opts=(TARGET_HOST ISCSI_TARGET_PORT WEB_PATH)
    for opt in "${opts[@]}"; do
        if [[ -z "${!opt}" ]]; then
            echo "$opt must be configured" 1>&2
            exit 1
        fi
    done
}

setup_nginx() {
    local target=$1; shift

    {
        cat <<END
server {
    server_name  $TARGET_HOST;
    default_type text/plain;
END
        for image_config in "$@"; do
            local name=$(config_name "$image_config")
            local path="$WEB_PATH/$name"
            mkdir -p "$path"
            touch "$path/index.html"
            cat <<END

    location /$name/ {
        root $WEB_PATH;
    }
END
        done
        cat <<END
}
END
    } > "$target"

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
        service iscsitarget restart && break
    done
}

if [[ "$#" -lt 2 ]]; then
    echo "usage: $0 <host config> <image config...>" >&2
    exit 1
fi

host_config=$1; shift

install_prereqs

load_host_config "$host_config"
setup_nginx /etc/nginx/sites-available/cow "$@"
setup_ietd
