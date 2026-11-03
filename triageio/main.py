import argparse
import os
import sys
from tabulate import tabulate
from triageio.processor import TelemetryProcessor
from triageio.analyzer import TelemetryAnalyzer

def main():
    parser = argparse.ArgumentParser(description='TriageIO - Statistical Processor for Enterprise Storage Telemetry')
    parser.add_argument('--results-dir', default='results', help='Path to results folder (default: results)')
    parser.add_argument('--output-csv', default='rma_analysis_report.csv', help='Output CSV file path')
    parser.add_argument('--rma-only', action='store_true', help='Only show RMA candidates')
    
    parser.add_argument('--gui', action='store_true', help='Launch the interactive dashboard')
    
    args = parser.parse_args()

    if args.gui:
        from triageio.desktop_app import launch_gui
        launch_gui()
        return

    if not os.path.isdir(args.results_dir):
        print(f"Error: Directory '{args.results_dir}' not found.")
        sys.exit(1)

    print(f"[*] Processing telemetry data from '{args.results_dir}'...")
    processor = TelemetryProcessor(args.results_dir)
    df = processor.process_files()

    if df.empty:
        print("[!] No telemetry data found in the results directory.")
        sys.exit(0)

    analyzer = TelemetryAnalyzer(df)
    
    # Statistical Summary
    stats_df = analyzer.calculate_summary_stats()
    print("\n[+] Statistical Summary (Metrics):")
    print(tabulate(stats_df, headers='keys', tablefmt='pretty', floatfmt=".2f"))

    # RMA Analysis
    rma_df = analyzer.identify_rma_candidates()
    
    # Outlier Analysis
    outliers_df = analyzer.identify_outliers(threshold=1.5) # Lower threshold for small sample
    
    print(f"\n[+] Processing complete. Total drives: {len(df)}")
    print(f"[+] Total potential RMA candidates: {len(rma_df)}")

    if not outliers_df.empty:
        print("\n[!] Statistical Outliers Identified (|Z-score| > 1.5):")
        print(tabulate(outliers_df, headers='keys', tablefmt='psql', showindex=False, floatfmt=".2f"))

    if args.rma_only:
        display_df = rma_df
        print("\n[!] Displaying ONLY RMA candidates:")
    else:
        display_df = df
        print("\n[+] Displaying all drive telemetry summary:")

    # Select columns for table display (cleaner overview)
    display_cols = ['Device', 'Model', 'Serial', 'Media Errors', 'Available Spare', 'Error Log Entries', 'Overall Status']
    print(tabulate(display_df[display_cols], headers='keys', tablefmt='psql', showindex=False))

    # Save detailed report
    df.to_csv(args.output_csv, index=False)
    print(f"\n[+] Detailed analysis report saved to: {args.output_csv}")

if __name__ == '__main__':
    main()
