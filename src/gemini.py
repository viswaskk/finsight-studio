# gemini_processor.py
"""
Pluggable Gemini Enrichment and Insights Generator for FinSight Studio
Leverages Gemini SDK to classify transaction details and generate coaching insights.
"""

import json
import os
import re
import sys
from datetime import datetime
from typing import List, Dict, Any, Optional

try:
    from google import genai
    from google.genai import types
except ImportError:
    genai = None
    types = None

DEFAULT_MODEL: str = "gemini-3.5-flash"


def call_gemini_api(
    api_key: str, 
    prompt: str, 
    model: str = DEFAULT_MODEL, 
    response_mime_type: str = "application/json"
) -> Optional[str]:
    """Invokes the Gemini Developer API using the official google-genai SDK."""
    if genai is None or types is None:
        print(
            "Warning: google-genai SDK is not installed. "
            "Please install it via `pip install google-genai` to use AI features.", 
            file=sys.stderr
        )
        return None
        
    try:
        client = genai.Client(api_key=api_key)
        config = types.GenerateContentConfig(
            response_mime_type=response_mime_type
        )
        response = client.models.generate_content(
            model=model,
            contents=prompt,
            config=config
        )
        return response.text
    except Exception as e:
        print(f"Warning: Error calling Gemini API via SDK: {e}", file=sys.stderr)
        return None


def clean_json_response(raw_text: Optional[str]) -> Optional[str]:
    """Cleans markdown code blocks around JSON responses if present."""
    if not raw_text:
        return None
    cleaned = raw_text.strip()
    # Remove markdown JSON block wrappers if Gemini leaked them in output formatting
    cleaned = re.sub(r'^```(?:json)?\s*', '', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'\s*```$', '', cleaned)
    return cleaned.strip()


def load_cache(cache_path: Optional[str]) -> Dict[str, Any]:
    """Loads the transaction mapping cache from a file."""
    if not cache_path or not os.path.exists(cache_path):
        return {}
    try:
        with open(cache_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"Warning: Could not load cache from {cache_path}: {e}", file=sys.stderr)
        return {}


def save_cache(cache: Dict[str, Any], cache_path: Optional[str]) -> None:
    """Saves the transaction mapping cache to a file."""
    if not cache_path:
        return
    try:
        parent_dir = os.path.dirname(os.path.abspath(cache_path))
        if parent_dir:
            os.makedirs(parent_dir, exist_ok=True)
            
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(cache, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"Warning: Could not save cache to {cache_path}: {e}", file=sys.stderr)


def enrich_transactions_with_gemini(
    transactions: List[Dict[str, Any]], 
    api_key: str, 
    categories_config: Dict[str, List[str]], 
    cache_path: str = "gemini_cache.json", 
    model: str = DEFAULT_MODEL
) -> List[Dict[str, Any]]:
    """
    Enriches raw transaction records by deduplicating descriptions, querying Gemini 
    in batches for categorization, clean merchant extraction, and counterparties, 
    while maintaining a local persistent JSON cache.
    """
    cache = load_cache(cache_path)
    
    # Find unique transaction descriptions that are not already cached
    unique_raw_descriptions = list(set(tx['description'] for tx in transactions))
    uncached_descriptions = [desc for desc in unique_raw_descriptions if desc not in cache]
    
    if uncached_descriptions:
        print(f"🤖 Found {len(uncached_descriptions)} uncached unique transaction descriptions (out of {len(unique_raw_descriptions)} total unique).")
        batch_size = 50
        categories_list = list(categories_config.keys())
        
        for start_idx in range(0, len(uncached_descriptions), batch_size):
            batch = uncached_descriptions[start_idx : start_idx + batch_size]
            print(f"   ➜ Processing batch of {len(batch)} descriptions ({start_idx + 1} to {min(start_idx + batch_size, len(uncached_descriptions))})...")
            
            prompt = f"""
You are a precise financial data intelligence assistant. Analyze the following raw credit card or bank transaction descriptions.
For each description, extract:
1. "category": Choose the best matching category from the Category List provided below. If none fit well, choose another standard category (e.g. 'Income', 'Investment', 'Transfer') or 'Uncategorized'.
2. "merchant": A clean, reader-friendly merchant/business name (e.g. 'Zomato', 'Amazon', 'Uber', 'Airtel', 'HDFC Bank'). Strip out all reference numbers, dates, cities, payment gateways (like Paytm, Razorpay) or transaction codes.
3. "counterparty": For transfer, UPI, or P2P records, extract the specific person or receiver name (e.g. 'John Doe'). For regular merchant transactions, set this to the same as the merchant.
4. "description_type": One of: 'merchant_purchase', 'peer_to_peer', 'salary', 'bank_charge', 'tax', 'refund', 'investment', 'transfer', 'unknown'.

Category List:
{json.dumps(categories_list, indent=2)}

Input Descriptions:
{json.dumps(batch, indent=2)}

Your output MUST be a single valid JSON object mapping each raw description to its extracted fields, strictly formatted like this:
{{
  "RAW_DESCRIPTION": {{
    "category": "Category Name",
    "merchant": "Clean Merchant Name",
    "counterparty": "Counterparty Name",
    "description_type": "merchant_purchase"
  }}
}}
Do not include markdown code blocks. Return ONLY the raw JSON.
"""
            raw_response = call_gemini_api(api_key, prompt, model=model, response_mime_type="application/json")
            cleaned_response = clean_json_response(raw_response)
            
            if cleaned_response:
                try:
                    batch_mappings = json.loads(cleaned_response)
                    for raw_desc, details in batch_mappings.items():
                        if isinstance(details, dict) and 'category' in details and 'merchant' in details:
                            cache[raw_desc] = {
                                'category': details.get('category', 'Uncategorized'),
                                'merchant': details.get('merchant', raw_desc),
                                'counterparty': details.get('counterparty', details.get('merchant', raw_desc)),
                                'description_type': details.get('description_type', 'unknown')
                            }
                except Exception as e:
                    print(f"Warning: Failed to parse JSON batch response: {e}", file=sys.stderr)
                    print(f"Raw response content was: {raw_response}", file=sys.stderr)
            else:
                print("Warning: Skipping batch due to empty/failed response.", file=sys.stderr)
                
        save_cache(cache, cache_path)
        print(f"✓ Persistent cache updated and saved to: {cache_path}")
    else:
        print("✓ All transaction descriptions resolved from local cache. Zero API calls made!")

    # Map cached values back into all transaction records
    enriched_transactions: List[Dict[str, Any]] = []
    for tx in transactions:
        raw_desc = tx['description']
        enriched_tx = tx.copy()
        
        if raw_desc in cache:
            details = cache[raw_desc]
            enriched_tx['category'] = details.get('category', 'Uncategorized')
            enriched_tx['merchant'] = details.get('merchant', raw_desc)
            enriched_tx['counterparty'] = details.get('counterparty', details.get('merchant', raw_desc))
            enriched_tx['description_type'] = details.get('description_type', 'unknown')
        else:
            enriched_tx['merchant'] = raw_desc
            enriched_tx['counterparty'] = raw_desc
            enriched_tx['description_type'] = 'unknown'
            
        enriched_transactions.append(enriched_tx)
        
    return enriched_transactions


def generate_gemini_insights(
    stats: Dict[str, Any], 
    transactions: List[Dict[str, Any]], 
    api_key: str, 
    model: str = DEFAULT_MODEL
) -> List[Dict[str, Any]]:
    """
    Generates highly personalized, smart spending coaching insights using Gemini LLM 
    based on overall aggregates and recent transaction profiles.
    """
    # Filter repayments out of coaching context
    context_txs = [
        {
            'date': tx['date'].strftime("%Y-%m-%d") if isinstance(tx['date'], datetime) else str(tx['date']),
            'description': tx['description'],
            'category': tx.get('category', 'Uncategorized'),
            'merchant': tx.get('merchant', 'Unknown'),
            'amount': tx['amount']
        }
        for tx in transactions if not tx.get('is_payment', False)
    ]
    
    # Sample recent transactions for safety limits
    context_sample = context_txs[:50]
    
    prompt = f"""
You are an elite personal wealth advisor and credit coach. Review the following credit card and bank spending analysis.
Generate 4 to 6 deep, highly personalized, smart, and actionable financial insights, budget warnings, or savings recommendations.

Analyze spending categories, month-over-month trends, transaction volumes, weekend spending spikes, and largest merchants.
For each insight:
- Highlight specific transaction values, percentage spikes, or merchant names to make it fully grounded in evidence.
- Provide a clear, friendly, actionable suggestion (e.g., 'You spent ₹X on Swiggy. Consider consolidating orders or using a co-branded card').
- Keep descriptions clean and format them with basic HTML tags like <strong> or <em> for key numbers, merchants, and categories.

Aggregated Spending Statistics:
- Net Spending: ₹ {stats.get('net_spending', 0.0):,.2f}
- Gross Outflows (Debits): ₹ {stats.get('total_debited', 0.0):,.2f}
- Gross Inflows (Refunds/Credits): ₹ {stats.get('total_credited', 0.0):,.2f}
- Total Transactions Analyzed: {stats.get('num_transactions', 0)}
- Average Monthly Outlay: ₹ {stats.get('avg_monthly_spend', 0.0):,.2f}
- Top Spend Categories (Sorted by Outlay): {json.dumps(stats.get('categories', [])[:4])}
- Top 5 Spend Merchants: {json.dumps(stats.get('merchants', [])[:5])}

Recent Transaction Profile (Sample of 50 records):
{json.dumps(context_sample, indent=2)}

Your output MUST be a single valid JSON array of insight items. Each item must contain:
1. "type": One of: "info" (standard tips), "warning" (overspending or anomalies), "success" (savings/positive trends), "danger" (critical charges/fees/critical issues).
2. "message": A rich, actionable recommendation formatted in safe HTML.

Strict JSON Array Format:
[
  {{
    "type": "warning",
    "message": "Your spending on <strong>Food & Dining</strong> reached ₹15,300..."
  }}
]
Do not include markdown blocks or formatting. Return ONLY the raw JSON string.
"""
    raw_response = call_gemini_api(api_key, prompt, model=model, response_mime_type="application/json")
    cleaned_response = clean_json_response(raw_response)
    
    if cleaned_response:
        try:
            insights = json.loads(cleaned_response)
            if isinstance(insights, list):
                valid_insights: List[Dict[str, Any]] = []
                for item in insights:
                    if isinstance(item, dict) and 'type' in item and 'message' in item:
                        valid_insights.append({
                            'type': item.get('type', 'info'),
                            'message': item.get('message', '')
                        })
                return valid_insights
        except Exception as e:
            print(f"Warning: Failed to parse JSON insights response: {e}", file=sys.stderr)
            print(f"Raw response was: {raw_response}", file=sys.stderr)
            
    return []
