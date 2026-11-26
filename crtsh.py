import sys
if sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')

import re
import psycopg
from psycopg.rows import dict_row 

db_conn_str = 'postgresql://guest@crt.sh:5432/certwatch'

try:
    print("[+] Connecting to crt.sh database via PgBouncer...")
    db_connection = psycopg.connect(db_conn_str, autocommit=True, row_factory=dict_row)
    db_cursor = db_connection.cursor()

    target_domain = 'dietrich.com'

    query = """
        SELECT DISTINCT 
            lower(ns.name_value) AS domain_name
        FROM 
            certificate_and_identities ns
        WHERE 
            ns.certificate_id IN (
                SELECT c.id 
                FROM certificate c 
                WHERE to_tsquery('certwatch', %s) @@ identities(c.certificate)
            )
            AND ns.name_value NOT LIKE '*%%'
            AND ns.name_value LIKE %s;
    """

    KEYWORDS = [
        "forti", "citrix", "vpn", "gateway", "pulse", "palo", "sophos", 
        "cisco", "asa", "f5", "anyconnect", "globalprotect", "netscaler",
        "pam", "cyberark", "beyondtrust", "thycotic", "delinea", "wallix",
        "rdp", "vnc", "ssh", "awingu", "guacamole"
    ]

    print(f"[+] Scanning certificate logs for '{target_domain}'...")
    
    # LIKE for "%.trendyol.com" or "%trendyol.com"
    db_cursor.execute(query, (target_domain, f"%.{target_domain}"))

    # print results
    print("\n[+] Findings:")
    all_domains = []
    matched_domains = []
    
    for row in db_cursor:
        domain = row['domain_name']
        all_domains.append(domain)
        print(f"  -> {domain}")
        
        # check for keyword matches safely (preventing false positives for short words like 'asa', 'pam', 'f5')
        matched_kws = []
        for kw in KEYWORDS:
            if kw in ["asa", "pam", "f5"]:
                # Match as a standalone token (delimited by dots, dashes, or start/end of string)
                # Also allow ciscoasa and cyberarkpam as safe compound words
                pattern = rf"\b{kw}\b"
                if kw == "asa":
                    pattern += "|ciscoasa"
                elif kw == "pam":
                    pattern += "|cyberarkpam"
                if re.search(pattern, domain):
                    matched_kws.append(kw)
            elif kw in domain:
                matched_kws.append(kw)
                
        if matched_kws:
            matched_domains.append((domain, matched_kws))

    print(f"\n[+] Total unique subdomain count: {len(all_domains)}")
    
    # Hassas ağ/servis kapısı olma ihtimali yüksek alt alan adlarını raporluyoruz
    print("\n[!] Findings with sensitive keywords:")
    if matched_domains:
        for domain, kws in matched_domains:
            print(f"  [ALERT] {domain} -> Matching Keywords: {', '.join(kws)}")
        print(f"\n[+] Total sensitive findings: {len(matched_domains)}")
    else:
        print("  [-] No sensitive findings found.")

except Exception as e:
    print(f"[-] Connection or Query Error: {e}")

finally:
    if 'db_connection' in locals():
        db_cursor.close()
        db_connection.close()
        print("\n[+] Database connection closed.")