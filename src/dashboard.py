#!/usr/bin/env python3
"""
Dashboard Generation Module for FinSight Studio
Aggregates statement records, runs analytical aggregates, and generates the interactive HTML dashboard.
"""

import argparse
import collections
import csv
import json
import os
import re
import statistics
import sys
import webbrowser
import shutil
from datetime import datetime
from typing import List, Dict, Any, Optional, Tuple

# Try to import the local Gemini processor
try:
    from src import gemini as gemini_processor
except ImportError:
    try:
        import gemini as gemini_processor
    except ImportError:
        try:
            import gemini_processor
        except ImportError:
            gemini_processor = None

# Try to import the local PDF parser engine
try:
    from src.parser import parse_pdf, sort_files_by_date, is_bill_payment, load_card_mappings
except ImportError:
    try:
        from parser import parse_pdf, sort_files_by_date, is_bill_payment, load_card_mappings
    except ImportError:
        try:
            from parse_statement import parse_pdf, sort_files_by_date, is_bill_payment, load_card_mappings
        except ImportError:
            DEFAULT_PAYMENT_PATTERNS: List[str] = [
                "CREDIT CARD PAYMENT",
                "NETBANKING TRANSFER",
                "AUTOPAY",
                "BBPS PAYMENT",
                "TELE TRANSFER CREDIT",
                "CC PAYMENT"
            ]

            def is_bill_payment(description: str, payment_patterns: Optional[List[str]] = None) -> bool:
                """Fallback definition if run outside the module directory."""
                if payment_patterns is None:
                    payment_patterns = DEFAULT_PAYMENT_PATTERNS
                desc_upper = description.upper()
                return any(pattern.upper() in desc_upper for pattern in payment_patterns)

            def load_card_mappings(path: Optional[str]) -> Dict[str, str]:
                """Fallback definition if run outside the module directory."""
                return {}


def load_categories(categories_path: Optional[str]) -> Dict[str, List[str]]:
    """Loads category-to-keyword mappings from JSON configuration."""
    if not categories_path:
        return {}
    if not os.path.exists(categories_path):
        fallback = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config", os.path.basename(categories_path))
        if os.path.exists(fallback):
            categories_path = fallback
        else:
            return {}
    try:
        with open(categories_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"Warning: Could not load categories: {e}", file=sys.stderr)
        return {}


def categorize_transaction(description: str, categories: Dict[str, List[str]]) -> str:
    """Assigns transaction category case-insensitively using keyword definitions."""
    desc_upper = description.upper()
    for category, patterns in categories.items():
        for pattern in patterns:
            if pattern.upper() in desc_upper:
                return category
    return "Uncategorized"


def get_clean_merchant(description: str, categories: Dict[str, List[str]]) -> str:
    """Extracts a clean, human-friendly merchant name from raw transaction description."""
    desc_upper = description.upper()
    
    # Match category patterns for high-quality names first
    for category, patterns in categories.items():
        for pattern in patterns:
            if pattern.upper() in desc_upper:
                return pattern.title()
                
    # Generic description cleaning rules
    clean = description
    
    # Eliminate transactional suffixes and cities
    clean = re.sub(
        r'\s+(VIA SMARTBUY|SMARTBUY|COM|CO|LTD|PVT|INDIA|GROUP|PRIVATE|LIMGURGAON|GURGAON|HYDERABAD|NEW DELHI|MUMBAI|BANGALORE|THANE|PORT BLAIR|PORTBLAIR|SAKLESHPUR).*$', 
        '', 
        clean, 
        flags=re.IGNORECASE
    )
    # Remove point balance markers (e.g., "+ 295")
    clean = re.sub(r'\s+[\+\-]\s*\d\s*$', '', clean)
    clean = re.sub(r'\s+[\+\-]\s*\d\s*,\s*\d\s*$', '', clean)
    # Remove reference and timestamp trailing digits
    clean = re.sub(r'\d{5,}.*$', '', clean)
    clean = clean.strip()
    
    if len(clean) > 3:
        clean = clean.title()
    return clean if clean else description


def standardize_transactions(
    transactions: List[Dict[str, Any]], 
    categories: Dict[str, List[str]]
) -> List[Dict[str, Any]]:
    """
    Standardizes transaction attributes, parses rewards points, cleans raw descriptions, 
    and extracts merchant/counterparty details.
    """
    standardized: List[Dict[str, Any]] = []
    payment_patterns = categories.get("Payment") or categories.get("__payments__")
    
    for tx in transactions:
        desc = str(tx.get('description', ''))
        amount = float(tx.get('amount', 0.0))
        date_obj = tx.get('date')
        
        # Force datetime format
        if isinstance(date_obj, str):
            try:
                date_obj = datetime.strptime(date_obj, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                try:
                    date_obj = datetime.strptime(date_obj, "%Y-%m-%d %H:%M")
                except ValueError:
                    date_obj = datetime.strptime(date_obj, "%Y-%m-%d")
        
        is_payment = is_bill_payment(desc, payment_patterns)
        card = tx.get('card', 'Unknown')
        account_type = tx.get('account_type', 'credit')
        
        if is_payment:
            standardized.append({
                'date': date_obj,
                'description': desc,
                'amount': amount,
                'points': int(tx.get('points', 0) or 0),
                'is_payment': True,
                'card': card,
                'account_type': account_type,
                'category': tx.get('category', 'Payment'),
                'merchant': tx.get('merchant', 'Payment'),
                'counterparty': tx.get('counterparty', 'Payment'),
                'description_type': tx.get('description_type', 'transfer')
            })
            continue
            
        # Extract point suffix values (e.g. HDFC card descriptions suffix "+ 295")
        match = re.search(r'\s+([\+\-]\s*\d+)\s*$', desc)
        if match:
            pts_str = match.group(1).replace(" ", "")
            try:
                points = int(pts_str)
            except ValueError:
                points = int(tx.get('points', 0) or 0)
            desc = desc[:match.start()].strip()
        else:
            points = int(tx.get('points', 0) or 0)
            
        standardized.append({
            'date': date_obj,
            'description': desc,
            'amount': amount,
            'points': points,
            'is_payment': False,
            'card': card,
            'account_type': account_type,
            'category': tx.get('category'),
            'merchant': tx.get('merchant'),
            'counterparty': tx.get('counterparty'),
            'description_type': tx.get('description_type')
        })
        
    return standardized


def calculate_analysis(
    transactions: List[Dict[str, Any]], 
    categories: Dict[str, List[str]]
) -> Dict[str, Any]:
    """
    Computes clean spending aggregations, monthly trends, categories distributions, 
    and merchant statistics for dashboard and AI engine usage.
    """
    # First standardize transactions attributes
    standardized_txs = standardize_transactions(transactions, categories)
    
    net_spending = 0.0
    total_debited = 0.0
    total_credited = 0.0
    total_points = 0
    
    monthly_spending = collections.defaultdict(float)
    monthly_credits = collections.defaultdict(float)
    category_spending = collections.defaultdict(float)
    category_monthly_spending = collections.defaultdict(lambda: collections.defaultdict(float))
    day_of_week_spending = collections.defaultdict(float)
    merchant_spending = collections.defaultdict(float)
    merchant_counts = collections.defaultdict(int)
    
    formatted_transactions: List[Dict[str, Any]] = []
    
    for tx in standardized_txs:
        amount = tx['amount']
        desc = tx['description']
        date_obj = tx['date']
        points = tx['points']
        is_payment = tx['is_payment']
        card = tx['card']
        account_type = tx['account_type']
        
        # Determine assigned category and merchant
        assigned_cat = tx.get('category') or (categorize_transaction(desc, categories) if not is_payment else 'Payment')
        assigned_merchant = tx.get('merchant') or (get_clean_merchant(desc, categories) if not is_payment else 'Payment')
        assigned_counterparty = tx.get('counterparty') or assigned_merchant or desc
        
        formatted_transactions.append({
            'date': date_obj.strftime("%Y-%m-%d %H:%M"),
            'description': desc,
            'points': points,
            'amount': amount,
            'category': assigned_cat,
            'is_payment': is_payment,
            'month': date_obj.strftime("%Y-%m"),
            'card': card,
            'merchant': assigned_merchant,
            'counterparty': assigned_counterparty,
            'description_type': tx.get('description_type') or 'unknown',
            'account_type': account_type
        })
        
        if is_payment:
            total_credited += amount
            continue
            
        total_points += points
        month_key = date_obj.strftime("%Y-%m")
        day_name = date_obj.strftime("%A")
        
        if amount < 0:
            # Outflow
            spent_amt = abs(amount)
            total_debited += spent_amt
            net_spending += spent_amt
            
            monthly_spending[month_key] += spent_amt
            category_spending[assigned_cat] += spent_amt
            category_monthly_spending[month_key][assigned_cat] += spent_amt
            day_of_week_spending[day_name] += spent_amt
            
            merchant_spending[assigned_merchant] += spent_amt
            merchant_counts[assigned_merchant] += 1
        else:
            # Inflow / Refund
            total_credited += amount
            net_spending -= amount
            monthly_credits[month_key] += amount
            
    # Chronological month options
    unique_months = sorted(list(set(monthly_spending.keys())))
    num_months = len(unique_months) if unique_months else 1
    avg_monthly_spend = net_spending / num_months
    
    months_options: List[Dict[str, str]] = []
    for m in unique_months:
        try:
            dt = datetime.strptime(m, "%Y-%m")
            m_name = dt.strftime("%B %Y")
        except Exception:
            m_name = m
        months_options.append({'value': m, 'label': m_name})
        
    # Volatility CV metric
    monthly_net = []
    for m in unique_months:
        monthly_net.append(monthly_spending[m] - monthly_credits.get(m, 0.0))
    
    volatility_cv = 0.0
    volatility_level = "Low"
    if len(monthly_net) >= 2:
        std_dev = statistics.stdev(monthly_net)
        mean_val = statistics.mean(monthly_net)
        if mean_val > 0:
            volatility_cv = (std_dev / mean_val) * 100.0
            if volatility_cv > 50.0:
                volatility_level = "High"
            elif volatility_cv > 25.0:
                volatility_level = "Moderate"
                
    # Sort categories and merchants
    sorted_categories = sorted(category_spending.items(), key=lambda x: x[1], reverse=True)
    sorted_merchants = sorted(merchant_spending.items(), key=lambda x: x[1], reverse=True)[:10]
    
    # Days spending standards
    days_order = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    day_spending_list = [day_of_week_spending.get(d, 0.0) for d in days_order]
    
    weekend_spend = day_of_week_spending.get("Saturday", 0.0) + day_of_week_spending.get("Sunday", 0.0)
    weekday_spend = sum(day_of_week_spending.values()) - weekend_spend
    weekend_ratio = (weekend_spend / (weekend_spend + weekday_spend) * 100.0) if (weekend_spend + weekday_spend) > 0 else 0.0
    
    return {
        'net_spending': net_spending,
        'total_debited': total_debited,
        'total_credited': total_credited,
        'total_points': total_points,
        'avg_monthly_spend': avg_monthly_spend,
        'num_transactions': len(formatted_transactions),
        'unique_months': unique_months,
        'months_options': months_options,
        'monthly_spending_values': [monthly_spending[m] for m in unique_months],
        'monthly_credits_values': [monthly_credits.get(m, 0.0) for m in unique_months],
        'categories': [c[0] for c in sorted_categories],
        'category_spending': [c[1] for c in sorted_categories],
        'days': days_order,
        'day_spending': day_spending_list,
        'merchants': [m[0] for m in sorted_merchants],
        'merchant_spending': [m[1] for m in sorted_merchants],
        'transactions': formatted_transactions,
        'categories_list': sorted(list(category_spending.keys())),
        'weekend_ratio': weekend_ratio,
        'volatility_cv': volatility_cv,
        'volatility_level': volatility_level
    }


def save_dashboard_json(data: Dict[str, Any], output_file: str) -> str:
    """Saves the computed dashboard analytical dataset into a decoupled JSON file."""
    dashboard_data = {
        'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M'),
        'transactions': data['transactions'],
        'categories_list': data['categories_list'],
        'months_options': data['months_options'],
        'unique_months': data['unique_months']
    }
    
    if 'gemini_insights' in data:
        dashboard_data['gemini_insights'] = data['gemini_insights']
    
    output_dir = os.path.dirname(os.path.abspath(output_file))
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)
        
    json_output_path = os.path.join(output_dir, "dashboard_data.json")
    
    with open(json_output_path, 'w', encoding='utf-8') as f:
        json.dump(dashboard_data, f, indent=2, ensure_ascii=False)
        
    print(f"✓ Spending analysis data saved to: {json_output_path}")
    return json_output_path


def serve_dashboard(html_path: str, no_open: bool = False) -> None:
    """Serves the dashboard directory via lightweight HTTP server."""
    import http.server
    import socketserver
    
    html_dir = os.path.dirname(os.path.abspath(html_path))
    html_file = os.path.basename(html_path)
    
    class QuietSimpleHTTPRequestHandler(http.server.SimpleHTTPRequestHandler):
        def end_headers(self) -> None:
            self.send_header('Access-Control-Allow-Origin', '*')
            super().end_headers()
        
        def log_message(self, format: str, *args: Any) -> None:
            pass
            
    original_cwd = os.getcwd()
    os.chdir(html_dir)
    
    port = 8000
    server_started = False
    
    while not server_started and port < 9000:
        try:
            socketserver.TCPServer.allow_reuse_address = True
            with socketserver.TCPServer(("", port), QuietSimpleHTTPRequestHandler) as httpd:
                server_started = True
                print(f"\n==========================================================")
                print(f"✓ FinSight Studio Analytics Server is now active!")
                print(f"  ➜  Dashboard: http://localhost:{port}/{html_file}")
                print(f"  ➜  Data API:  http://localhost:{port}/dashboard_data.json")
                print(f"==========================================================\n")
                print("Press Ctrl+C to stop the local server and exit.")
                
                if not no_open:
                    webbrowser.open(f"http://localhost:{port}/{html_file}")
                
                try:
                    httpd.serve_forever()
                except KeyboardInterrupt:
                    print("\nStopping Spend Analytics local server...")
        except OSError:
            port += 1
        finally:
            if server_started:
                os.chdir(original_cwd)


def main() -> None:
    """CLI orchestration wrapper for dashboard builder."""
    parser = argparse.ArgumentParser(description="Generate premium financial analytics HTML dashboard")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--csv", help="Path to pre-parsed transaction CSV file")
    group.add_argument("--dir", help="Path to directory containing PDF statements")
    group.add_argument("--file", help="Path to a single PDF statement")
    group.add_argument("--json", help="Path to existing dashboard JSON data to serve")
    
    parser.add_argument("--name", help="Cardholder name (required for PDF parsing)")
    parser.add_argument("--password", default="", help="PDF password (if statements are encrypted)")
    parser.add_argument("--categories", default="categories.json", help="Path to categories JSON file")
    parser.add_argument("--cards", default="cards.json", help="Path to cards mapping JSON file")
    parser.add_argument("--output", default="dashboard.html", help="Output path for the HTML dashboard")
    parser.add_argument("--sortformat", help="Date format in PDF filenames for sorting (e.g., %%d-%%m-%%Y)")
    parser.add_argument("--no-open", action="store_true", help="Do not open dashboard in browser automatically")
    parser.add_argument("--no-serve", action="store_true", help="Do not start the local HTTP web server")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging for PDF parsing")
    
    # Pluggable Gemini Configurations
    parser.add_argument("--gemini-key", help="Gemini API Key (can also set GEMINI_API_KEY env var)")
    parser.add_argument("--gemini-cache", default="gemini_cache.json", help="Path to Gemini descriptions cache file")
    parser.add_argument("--gemini-model", default="gemini-3.5-flash", help="Gemini model to use")
    
    args = parser.parse_args()
    
    if args.json:
        if not os.path.exists(args.json):
            print(f"Error: JSON file not found: {args.json}", file=sys.stderr)
            sys.exit(1)
            
        output_dir = os.path.dirname(os.path.abspath(args.output))
        target_json = os.path.join(output_dir, "dashboard_data.json")
        
        if os.path.abspath(args.json) != os.path.abspath(target_json):
            try:
                shutil.copy2(args.json, target_json)
                print(f"✓ Copied existing JSON data from {args.json} to {target_json}")
            except Exception as e:
                print(f"Error copying JSON data: {e}", file=sys.stderr)
                sys.exit(1)
        else:
            print(f"✓ Using existing JSON data at {target_json}")
            
        # Copy HTML static template to output folder
        script_dir = os.path.dirname(os.path.abspath(__file__))
        template_path = os.path.join(os.path.dirname(script_dir), "templates", "dashboard.html")
        
        if os.path.exists(template_path) and os.path.abspath(template_path) != os.path.abspath(args.output):
            try:
                shutil.copy2(template_path, args.output)
                print(f"✓ Copied static HTML dashboard template to: {args.output}")
            except Exception as e:
                print(f"Warning: Could not copy static HTML template: {e}", file=sys.stderr)
                
        if not args.no_serve:
            serve_dashboard(args.output, no_open=args.no_open)
        else:
            if not args.no_open:
                abs_path = os.path.abspath(args.output)
                print(f"Opening dashboard in default browser...")
                webbrowser.open(f"file://{abs_path}")
        sys.exit(0)
        
    # Load custom structures
    categories = load_categories(args.categories)
    cards_mapping = load_card_mappings(args.cards)
    
    transactions: List[Dict[str, Any]] = []
    
    if args.csv:
        if not os.path.exists(args.csv):
            print(f"Error: CSV file not found: {args.csv}", file=sys.stderr)
            sys.exit(1)
            
        print(f"Loading transactions from CSV: {args.csv}")
        with open(args.csv, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    date_obj = datetime.strptime(row['Date'], "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    try:
                        date_obj = datetime.strptime(row['Date'], "%Y-%m-%d %H:%M")
                    except ValueError:
                        date_obj = datetime.strptime(row['Date'], "%Y-%m-%d")
                        
                card_name = row.get('Card', 'Unknown')
                account_type = "bank" if "sbi" in card_name.lower() or "savings" in card_name.lower() or "account" in card_name.lower() else "credit"
                transactions.append({
                    'date': date_obj,
                    'description': row['Description'],
                    'points': int(row.get('Points', 0) or 0),
                    'amount': float(row['Amount']),
                    'card': card_name,
                    'account_type': account_type
                })
    else:
        if not args.name:
            print("Error: --name is required when parsing PDF statements directly.", file=sys.stderr)
            sys.exit(1)
            
        pdf_files: List[str] = []
        if args.dir:
            if not os.path.isdir(args.dir):
                print(f"Error: Directory not found: {args.dir}", file=sys.stderr)
                sys.exit(1)
            for root, dirs, files in os.walk(args.dir):
                for file in files:
                    if file.lower().endswith(".pdf"):
                        pdf_files.append(os.path.join(root, file))
            if not pdf_files:
                print(f"Error: No PDF files found in directory {args.dir}", file=sys.stderr)
                sys.exit(1)
        elif args.file:
            if not os.path.exists(args.file):
                print(f"Error: File not found: {args.file}", file=sys.stderr)
                sys.exit(1)
            pdf_files.append(args.file)
            
        if args.sortformat and pdf_files:
            sort_files_by_date(pdf_files, args.sortformat)
            
        print(f"Found {len(pdf_files)} statement PDF(s). Parsing...")
        
        try:
            for file_path in pdf_files:
                print(f"  Parsing: {os.path.basename(file_path)}")
                parsed_txs = parse_pdf(file_path, args.name, args.password, cards_mapping=cards_mapping, debug=args.debug)
                for tx in parsed_txs:
                    transactions.append({
                        'date': tx.date,
                        'description': tx.description,
                        'points': tx.points,
                        'amount': tx.amount,
                        'card': tx.card,
                        'account_type': getattr(tx, 'account_type', 'credit')
                    })
        except NameError:
            print("Error: PDF parsing is not available because parse_statement.py is missing or invalid.", file=sys.stderr)
            sys.exit(1)
            
    if not transactions:
        print("No transactions loaded or extracted. Exiting.", file=sys.stderr)
        sys.exit(1)
        
    print(f"Successfully loaded {len(transactions)} transaction records.")
    transactions.sort(key=lambda x: x['date'])
    
    # Pluggable Gemini API Enrichment Hooks
    api_key = args.gemini_key or os.environ.get("GEMINI_API_KEY")
    gemini_insights = []
    
    if api_key and gemini_processor:
        print(f"🤖 Pluggable Gemini enabled. Enriching transactions using Gemini ({args.gemini_model})...")
        try:
            enriched_txs = gemini_processor.enrich_transactions_with_gemini(
                transactions, 
                api_key, 
                categories, 
                cache_path=args.gemini_cache, 
                model=args.gemini_model
            )
            transactions = enriched_txs
        except Exception as e:
            print(f"Warning: Gemini transaction enrichment failed: {e}. Falling back to offline analytics.", file=sys.stderr)
    else:
        print("ℹ️ No Gemini API key provided or processor unavailable. Using offline rules classification.")

    # Calculate spending aggregates
    analysis_data = calculate_analysis(transactions, categories)
    
    # Pluggable Gemini Spending Insights
    if api_key and gemini_processor:
        print("🤖 Generating Smart Spending Insights via Gemini...")
        try:
            gemini_insights = gemini_processor.generate_gemini_insights(
                analysis_data, 
                transactions, 
                api_key, 
                model=args.gemini_model
            )
            if gemini_insights:
                print(f"✓ Successfully generated {len(gemini_insights)} smart Gemini insights!")
                analysis_data["gemini_insights"] = gemini_insights
        except Exception as e:
            print(f"Warning: Gemini smart insights generation failed: {e}. Falling back to offline dashboard.", file=sys.stderr)
            
    # Serialize analysis data to JSON
    save_dashboard_json(analysis_data, args.output)
    
    # Copy static template HTML if needed
    script_dir = os.path.dirname(os.path.abspath(__file__))
    template_path = os.path.join(os.path.dirname(script_dir), "templates", "dashboard.html")
    
    if os.path.exists(template_path) and os.path.abspath(template_path) != os.path.abspath(args.output):
        try:
            shutil.copy2(template_path, args.output)
            print(f"✓ Copied static HTML dashboard template to: {args.output}")
        except Exception as e:
            print(f"Warning: Could not copy static HTML template: {e}", file=sys.stderr)
            
    # Serve dashboard locally
    if not args.no_serve:
        serve_dashboard(args.output, no_open=args.no_open)
    else:
        if not args.no_open:
            abs_path = os.path.abspath(args.output)
            print(f"Opening dashboard in default browser...")
            webbrowser.open(f"file://{abs_path}")


if __name__ == "__main__":
    main()
