import pandas as pd
import os
import re
import numpy as np

# ======================================================================
#  VENDOR-AGNOSTIC SMART FIELD ALIAS MAP
#  Maps every known variant name -> canonical TriageIO field name.
#  Covers: NVMe (all vendors), SATA (smartctl/CrystalDiskInfo/vendor tools),
#          SAS, OCP Datacenter SSD Spec v2.5, Samsung, Micron, WD, Seagate,
#          Kioxia, SK Hynix, Intel/Solidigm naming conventions.
# ======================================================================
FIELD_ALIASES: dict[str, str] = {
    # --- Temperature ---
    "temperature":                       "Temperature",
    "temperature (c)":                   "Temperature",
    "temperature_celsius":               "Temperature",
    "composite temperature":             "Temperature",
    "temperature sensor 1":              "Temperature",
    "temperature value":                 "Temperature",
    "drive temperature":                 "Temperature",
    "hda temperature":                   "Temperature",
    "airflow temperature cel":           "Temperature",
    "airflow_temperature_cel":           "Temperature",
    "temp":                              "Temperature",
    "thermal":                           "Temperature",
    # --- Available Spare ---
    "available spare":                   "Available Spare",
    "available_spare":                   "Available Spare",
    "spare available":                   "Available Spare",
    "reserved spare":                    "Available Spare",
    "spare block count":                 "Available Spare",
    "spare block remaining":             "Available Spare",
    "nand spare":                        "Available Spare",
    "available spare threshold":         "Available Spare",   # fallback
    # --- Endurance Used ---
    "endurance used":                    "Endurance Used",
    "percentage used":                   "Endurance Used",
    "percent_used":                      "Endurance Used",
    "life used":                         "Endurance Used",
    "life remaining":                    "_life_remaining",   # inverted — handled below
    "wear indicator":                    "Endurance Used",
    "wear leveling count":               "Endurance Used",
    "ssd life left":                     "_life_remaining",
    "media wearout indicator":           "Endurance Used",
    "nand endurance":                    "Endurance Used",
    "drive life used":                   "Endurance Used",
    "used endurance indicator":          "Endurance Used",
    "percentage_used":                   "Endurance Used",
    # --- Media Errors ---
    "media errors":                      "Media Errors",
    "media and data integrity errors":   "Media Errors",
    "media_and_data_integrity_errors":   "Media Errors",
    "uncorrectable error count":         "Media Errors",
    "uncorrectable errors":              "Media Errors",
    "uecc count":                        "Media Errors",
    "raw read error rate":               "Media Errors",   # SATA proxy
    "reallocated sector ct":             "Media Errors",   # SATA proxy
    "reallocated_sector_ct":             "Media Errors",
    "uncorrectable sector count":        "Media Errors",
    "offline uncorrectable":             "Media Errors",
    "ecc error count":                   "Media Errors",
    # --- Unsafe Shutdowns ---
    "unsafe shutdowns":                  "Unsafe Shutdowns",
    "unsafe_shutdowns":                  "Unsafe Shutdowns",
    "power loss count":                  "Unsafe Shutdowns",
    "power-off retract count":           "Unsafe Shutdowns",
    "power_off_retract_count":           "Unsafe Shutdowns",
    "unexpected power loss count":       "Unsafe Shutdowns",
    "host controlled thermal mgmt":      "Unsafe Shutdowns",  # approximate
    "unsafe shutdown count":             "Unsafe Shutdowns",
    "sudden power loss count":           "Unsafe Shutdowns",
    "program fail count total":          "Unsafe Shutdowns",  # loose SATA proxy
    # --- Power On Hours ---
    "power on hours":                    "Power On Hours",
    "power_on_hours":                    "Power On Hours",
    "power on time":                     "Power On Hours",
    "power-on hours":                    "Power On Hours",
    "power-on hours count":              "Power On Hours",
    "power on time count":               "Power On Hours",
    "drive power on time":               "Power On Hours",
    "hours":                             "Power On Hours",
    "poh":                               "Power On Hours",
    # --- Data Units Written ---
    "data units written":                "Data Units Written",
    "data_units_written":                "Data Units Written",
    "total data written":                "Data Units Written",
    "data written":                      "Data Units Written",
    "total lbas written":                "Data Units Written",
    "total bytes written":               "Data Units Written",
    "nand bytes written":                "Data Units Written",
    "host writes":                       "Data Units Written",
    "host write commands":               "Data Units Written",
    "lifetime writes from host":         "Data Units Written",
    "total write sectors":               "Data Units Written",
    # --- Error Log Entries ---
    "error log entries":                 "Error Log Entries",
    "error_log_entries":                 "Error Log Entries",
    "number of error log entries":       "Error Log Entries",
    "error information log entries":     "Error Log Entries",
    "controller busy time":              "Error Log Entries",  # loose proxy
    "command timeout count":             "Error Log Entries",
    "reported uncorrect":                "Error Log Entries",
    "error count":                       "Error Log Entries",
    # --- Model ---
    "model":                             "Model",
    "model number":                      "Model",
    "drive model":                       "Model",
    "product":                           "Model",
    "device model":                      "Model",
    # --- Serial ---
    "serial":                            "Serial",
    "serial number":                     "Serial",
    "serial_number":                     "Serial",
    "drive serial":                      "Serial",
    # --- Firmware ---
    "firmware":                          "Firmware",
    "firmware revision":                 "Firmware",
    "firmware version":                  "Firmware",
    "fw version":                        "Firmware",
    "fw rev":                            "Firmware",
    # --- Device ---
    "device":                            "Device",
    "device path":                       "Device",
    "nvme device":                       "Device",
}


class TelemetryProcessor:
    """MASTER ENGINE v3.0: Vendor-agnostic recursive SMART telemetry extractor."""
    def __init__(self, results_dir):
        self.results_dir = results_dir
        self.metadata = {
            "total_files_found": 0,
            "parsed_successfully": 0,
            "skipped_files": 0,
            "subfolders_processed": set(),
            "errors": []
        }

    def _clean_numeric(self, value):
        if pd.isna(value): return 0.0
        try:
            raw = str(value).strip().replace(',', '').replace('%', '').split(' ')[0]
            match = re.search(r'([-?\d.]+)', raw)
            return float(match.group(1)) if match else 0.0
        except: return 0.0

    def _resolve_field(self, m_map: dict, canonical: str, default=None):
        """
        Looks up a canonical field name in a raw metric map, trying direct match
        first and then the full FIELD_ALIASES table (case-insensitive).
        Handles inverted 'life remaining' fields (100 - value).
        """
        # 1. Direct match (tool already wrote canonical name)
        if canonical in m_map:
            return m_map[canonical]
        # 2. Alias scan
        for raw_key, mapped in FIELD_ALIASES.items():
            is_inverted = (canonical == "Endurance Used" and mapped == "_life_remaining")
            if mapped == canonical or is_inverted:
                # try exact then case-insensitive
                if raw_key in m_map:
                    val = m_map[raw_key]
                    if is_inverted:
                        return max(0.0, 100.0 - self._clean_numeric(val))
                    return val
        return default

    def _classify(self, model: str, device: str = "") -> str:
        """Classifies drive into a standardized tier based on model name and device path."""
        m = str(model).upper()
        d = str(device).upper()
        # Interface detection
        is_nvme = ('NVME' in m or 'NVME' in d or 'PCIe' in d.upper() or 'M.2' in m)
        is_sata = ('SATA' in m or 'ATA' in d or 'SATA' in d)
        is_sas  = ('SAS' in m or 'SAS' in d or 'SCSI' in d)
        # NAND type detection
        is_qlc  = 'QLC' in m
        is_cons = any(k in m for k in ('CONSUMER', 'CLIENT', 'HIGHPERF', 'MX', 'BX', 'EVO', 'PRO', 'PLUS'))
        is_ent  = any(k in m for k in ('ENT', 'ENTERPRISE', 'P3', 'P4', 'P5', 'D3', 'D5',
                                        'PM9', 'PM10', 'HAWK', 'FALCON', 'PELICAN', 'VULTURE'))
        if is_sas:
            return 'SAS Enterprise'
        if is_nvme and is_qlc:
            return 'NVMe QLC'
        if is_nvme and is_ent:
            return 'NVMe Enterprise'
        if is_nvme:
            return 'NVMe Enterprise' if not is_cons else 'NVMe Consumer'
        if is_sata and is_qlc:
            return 'SATA QLC High-Cap'
        if is_sata:
            return 'SATA Enterprise' if is_ent else ('Consumer/Client' if is_cons else 'SATA Enterprise')
        if is_qlc:
            return 'QLC High-Cap'
        if is_cons:
            return 'Consumer/Client'
        return 'Standard Tier'

    def _detect_interface(self, model: str, device: str, serial: str) -> str:
        """Returns a human-readable interface string."""
        m, d = str(model).upper(), str(device).upper()
        if 'NVME' in m or 'NVME' in d or 'PCIE' in d:
            if 'GEN5' in m or 'GEN5' in d: return 'NVMe PCIe Gen5'
            if 'GEN4' in m or 'GEN4' in d: return 'NVMe PCIe Gen4'
            if 'GEN3' in m or 'GEN3' in d: return 'NVMe PCIe Gen3'
            return 'NVMe PCIe'
        if 'SAS' in m or 'SAS' in d:       return 'SAS 12Gbps'
        if 'SATA' in m or 'ATA' in d:      return 'SATA 6Gbps'
        return 'Unknown'

    def process_files(self):
        data_list = []
        if not os.path.exists(self.results_dir): return pd.DataFrame()
        
        all_files = []
        for root, dirs, files in os.walk(self.results_dir):
            for file in files:
                if file.endswith('.xlsx'):
                    all_files.append(os.path.join(root, file))
                    self.metadata["subfolders_processed"].add(root)
        
        self.metadata["total_files_found"] = len(all_files)
        all_files.sort()
        
        for idx, file_path in enumerate(all_files):
            try:
                df = pd.read_excel(file_path, engine='openpyxl')
                if df.empty:
                    self.metadata["skipped_files"] += 1
                    continue

                # Build a case-insensitive raw metric map
                if 'Metric' in df.columns and 'Value' in df.columns:
                    m_map = {str(r['Metric']).strip(): r['Value'] for _, r in df.iterrows()}
                elif len(df.columns) >= 2:
                    # Auto-detect: first col = metric name, second col = value
                    m_map = {str(r.iloc[0]).strip(): r.iloc[1] for _, r in df.iterrows()}
                else:
                    self.metadata["skipped_files"] += 1
                    continue

                # Also build lowercase-keyed map for alias resolution
                m_map_lower = {k.lower().strip(): v for k, v in m_map.items()}
                # Merge both so _resolve_field can find either
                m_map_full = {**m_map, **m_map_lower}

                model  = str(self._resolve_field(m_map_full, 'Model',  m_map_full.get('model', 'Unknown')))
                device = str(self._resolve_field(m_map_full, 'Device', m_map_full.get('device', 'Unknown')))
                serial = str(self._resolve_field(m_map_full, 'Serial', m_map_full.get('serial', 'Unknown')))
                fw     = str(self._resolve_field(m_map_full, 'Firmware', m_map_full.get('firmware', 'Unknown')))

                def rf_num(canonical, default=0):
                    v = self._resolve_field(m_map_full, canonical, default)
                    return self._clean_numeric(v)

                # relpath fails on Windows when source/dest are on different drives
                try:
                    src_path = os.path.relpath(file_path, self.results_dir)
                except ValueError:
                    src_path = file_path

                record = {
                    'Batch_Idx':        idx + 1,
                    'Source_Path':      src_path,
                    'Device':           device,
                    'Interface':        self._detect_interface(model, device, serial),
                    'Model':            model,
                    'Drive Class':      self._classify(model, device),
                    'Serial':           serial,
                    'Firmware':         fw,
                    'Temperature':      rf_num('Temperature',      0),
                    'Available Spare':  rf_num('Available Spare',  100),
                    'Endurance Used':   rf_num('Endurance Used',   0),
                    'Media Errors':     rf_num('Media Errors',     0),
                    'Unsafe Shutdowns': rf_num('Unsafe Shutdowns', 0),
                    'Power On Hours':   rf_num('Power On Hours',   0),
                    'Data Units Written': rf_num('Data Units Written', 0),
                    'Error Log Entries': rf_num('Error Log Entries',  0),
                }

                # Multi-tier status logic (JEDEC JESD218B thresholds)
                if record['Media Errors'] > 0 or record['Available Spare'] < 10:
                    record['Overall Status'] = 'ERROR'
                elif (record['Available Spare'] < 90
                      or record['Temperature'] > 70
                      or record['Endurance Used'] > 90
                      or record['Error Log Entries'] > 1000):
                    record['Overall Status'] = 'WARNING'
                else:
                    record['Overall Status'] = 'OK'

                data_list.append(record)
                self.metadata["parsed_successfully"] += 1
            except Exception as e:
                self.metadata["skipped_files"] += 1
                self.metadata["errors"].append(f"{os.path.basename(file_path)}: {str(e)}")
                continue
                
        return pd.DataFrame(data_list)

