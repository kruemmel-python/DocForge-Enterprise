import http.server
import socketserver
import json
import struct
import base64
import sys
import os

# --- Engine laden ---
# Wir nutzen die Klasse aus deiner existierenden Datei
try:
    from mycelia_chat_engine import MyceliaChatEngine
except ImportError:
    print("FEHLER: mycelia_chat_engine.py nicht gefunden!")
    sys.exit(1)

# --- Konfiguration ---
PORT = 9999
# Dieses Secret muss sicher sein! Es schützt die Seeds.
APP_SECRET = "MyceliaEnterpriseSecretKey2025"

print("[Proxy] Initialisiere GPU Engine...")
try:
    # GPU 0 initialisieren
    engine = MyceliaChatEngine(0)
    engine.set_password(APP_SECRET)
    print("[Proxy] GPU bereit. Warte auf PHP-Anfragen...")
except Exception as e:
    print(f"[Critical] GPU Fehler: {e}")
    sys.exit(1)

class MyceliaRequestHandler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        # 1. Daten von PHP empfangen
        content_length = int(self.headers['Content-Length'])
        post_data = self.rfile.read(content_length)
        
        try:
            req = json.loads(post_data.decode('utf-8'))
            action = req.get('action')
            response = {'status': 'error', 'message': 'Unknown action'}

            if action == 'encrypt':
                # PHP sendet Klartext -> Wir senden Seed + Blob zurück
                plaintext = req.get('data', '')
                
                # Verschlüsseln via GPU (Format: Seed[8] + Len[4] + Data)
                packet = engine.encrypt_text(plaintext)
                
                # Paket zerlegen für SQL-Datenbank
                seed_int = struct.unpack("Q", packet[:8])[0]
                cipher_part = packet[8:] # Rest als Blob
                
                # Base64 für sicheren Transport zu PHP
                blob_b64 = base64.b64encode(cipher_part).decode('utf-8')
                
                response = {
                    'status': 'ok',
                    'seed': str(seed_int), # Als String wegen 64-Bit Integer in JS/PHP
                    'blob': blob_b64
                }
                print(f"[Log] Encrypt Request: Seed {seed_int} generiert.")

            elif action == 'decrypt':
                # PHP sendet Seed + Blob -> Wir senden Klartext
                seed_int = int(req.get('seed'))
                blob_b64 = req.get('blob')
                
                blob_bytes = base64.b64decode(blob_b64)
                
                # Paket rekonstruieren
                packet = struct.pack("Q", seed_int) + blob_bytes
                
                # Entschlüsseln via GPU
                plaintext = engine.decrypt_packet(packet)
                
                response = {
                    'status': 'ok',
                    'data': plaintext
                }
                print(f"[Log] Decrypt Request: Seed {seed_int} verarbeitet.")

            # Antwort senden
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(response).encode('utf-8'))

        except Exception as e:
            print(f"[Error] {e}")
            self.send_response(500)
            self.end_headers()

# Server starten
with socketserver.TCPServer(("127.0.0.1", PORT), MyceliaRequestHandler) as httpd:
    print(f"[Proxy] Listening on localhost:{PORT}")
    httpd.serve_forever()