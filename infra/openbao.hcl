ui = false
disable_mlock = false
api_addr = "https://openbao:8200"

storage "file" {
  path = "/var/lib/openbao"
}

listener "tcp" {
  address = "0.0.0.0:8200"
  tls_cert_file = "/etc/openbao/certs/server.crt"
  tls_key_file = "/etc/openbao/certs/server.key"
  tls_min_version = "tls12"
}
