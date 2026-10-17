#!/usr/bin/env bash
source "$(dirname -- "$0")/common.sh"
assert_cluster; need skopeo
manifest=${1:?Usage: cache-images.sh rendered-manifest.yaml}
mkdir -p "$ROOT/.cache/images"
python3 "$ROOT/scripts/image-list.py" "$manifest" | while IFS= read -r image; do
  [[ -n $image ]] || continue
  available=true
  for node in $(kind get nodes --name "$CLUSTER"); do
    if ! docker exec "$node" crictl inspecti "$image" >/dev/null 2>&1; then available=false; fi
  done
  # Locally built addon images loaded into every node do not need a remote registry.
  if $available; then continue; fi
  cache_key=$(printf '%s' "$image" | sha256sum | cut -d' ' -f1)
  archive="$ROOT/.cache/images/$cache_key.tar"
  if [[ ! -s $archive ]]; then
    echo "Caching $image through host registry access"
    python3 "$ROOT/scripts/fetch-image.py" "$image" "$archive"
  fi
  # Import OCI archives with original digest and tag aliases, then verify CRI sees the requested reference.
  for node in $(kind get nodes --name "$CLUSTER"); do
    if docker exec "$node" crictl inspecti "$image" >/dev/null 2>&1; then continue; fi
    docker exec -i "$node" ctr --namespace=k8s.io images import --all-platforms --digests - < "$archive"
    if [[ $image == *@sha256:* ]]; then
      repository=${image%@*}
      if [[ ${repository##*/} == *:* ]]; then repository=${repository%:*}; fi
      import_source="$image"
      if ! docker exec "$node" ctr --namespace=k8s.io images ls -q | grep -Fxq "$import_source"; then
        import_source=${image%@*}
      fi
      imported_digest=$(docker exec "$node" ctr --namespace=k8s.io images ls "name==$import_source" | awk 'NR==2 {print $3}')
      [[ $imported_digest == "${image##*@}" ]] || die "Imported archive digest does not match $image"
      docker exec "$node" ctr --namespace=k8s.io images tag --force "$import_source" "$repository@${image##*@}"
    fi
    for ((attempt=0;attempt<10;attempt++)); do
      if docker exec "$node" crictl inspecti "$image" >/dev/null 2>&1; then break; fi
      sleep 1
    done
    docker exec "$node" crictl inspecti "$image" >/dev/null || die "Image alias $image missing on $node"
  done
done
