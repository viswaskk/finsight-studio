#!/usr/bin/env python3
"""
Statement Parser Engine for FinSight Studio
Parses HDFC Credit Cards/Bank Statements, ICICI Credit Cards, and SBI Savings Accounts.
"""

import argparse
import csv
from datetime import datetime
import json
import os
import re
import sys
from typing import List, Dict, Any, Optional, Union, Pattern, Set

import pypdf


class Transaction:
    """Represents a standardized transaction record parsed from bank or credit statements."""
    
    def __init__(
        self, 
        date: Optional[datetime] = None, 
        description: str = "", 
        points: int = 0, 
        amount: float = 0.0, 
        card: str = "Unknown", 
        account_type: str = "credit"
    ) -> None:
        self.date: datetime = date or datetime(1970, 1, 1)
        self.description: str = description
        self.points: int = points
        self.amount: float = amount
        self.card: str = card
        self.account_type: str = account_type
        self.balance: Optional[float] = None

    def __repr__(self) -> str:
        return (
            f"Transaction(date={self.date.strftime('%Y-%m-%d %H:%M')}, "
            f"description={self.description!r}, points={self.points}, "
            f"amount={self.amount}, card={self.card!r}, "
            f"account_type={self.account_type!r})"
        )


# Standard date format templates observed in PDF statements
DATE_FORMATS: List[str] = [
    "%d/%m/%Y| %H:%M",   # Domestic HDFC Infinia: "19/10/2025| 00:57"
    "%d/%m/%Y | %H:%M",  # International HDFC Infinia: "26/09/2025 | 13:33"
    "%d/%m/%Y %H:%M:%S", # Old formats with seconds
]


def parse_transaction_date(s: str) -> Optional[datetime]:
    """Attempts to parse a date string into a datetime object using known formats."""
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    try:
        return datetime.strptime(s, "%d/%m/%Y")
    except ValueError:
        return None


def parse_amount(s: str, is_credit: bool) -> Optional[float]:
    """Parses amount string to float, setting negative values for debits/spends."""
    clean = s.replace('₹', '').replace('\u20b9', '').replace(',', '').strip()
    if not clean:
        return None
    if clean.startswith('+'):
        is_credit = True
        num_str = clean[1:].strip()
    else:
        num_str = clean
    
    try:
        amt = float(num_str)
        return amt if is_credit else -amt
    except ValueError:
        return None


def parse_points(s: str) -> Optional[int]:
    """Extracts reward points earned or reversed in a transaction."""
    clean = s.replace("+ ", "+").replace("- ", "-").strip()
    try:
        return int(clean)
    except ValueError:
        return None


# Text tokens indicating the end of the transaction ledger list in statements
SECTION_TERMINATORS: Set[str] = {
    "Eligible for EMI",
    "Eligible for",
    "TRANSACTIONS",
    "Past Dues",
    "GST Summary",
    "Rewards Program Points Summary",
    "Offers on your card",
    "TOTAL AMOUNT",
    "CONVERT TO EMI",
}

FOREIGN_CURRENCY_PREFIXES = ("USD ", "JPY ", "MYR ", "EUR ", "GBP ", "SGD ", "AUD ", "THB ")


def is_section_terminator(text: str) -> bool:
    """Checks if a line of text signifies the end of the transaction ledger section."""
    cleaned = re.sub(r'\s+', ' ', text).strip()
    for term in SECTION_TERMINATORS:
        if cleaned.startswith(term):
            return True
    return text.startswith("*Transaction time")


def is_page_header(text: str) -> bool:
    """Checks if a text block is a standard page header to skip it during parsing."""
    return (
        text == "Infinia Credit Card Statement"
        or text.startswith("HSN Code:")
        or text.startswith("HDFC Bank Credit Cards GSTIN:")
        or "GSTIN: 33AAACH" in text
    )


def is_page_number(text: str) -> bool:
    """Detects page number strings to exclude from descriptions."""
    return text.startswith("Page ") and " of " in text


def is_foreign_currency(text: str) -> bool:
    """Detects international currencies to treat them as part of transaction descriptions."""
    return text.startswith(FOREIGN_CURRENCY_PREFIXES)


SKIPPABLE_SYMBOLS: Set[str] = {"+", "C", "₹", "l", "•", "●", "Cr"}


def is_skippable_symbol(text: str) -> bool:
    """Identifies standalone symbols that do not provide analytical utility."""
    return text in SKIPPABLE_SYMBOLS


class ParserState:
    """Manages tokenizing state-machine during custom coordinate-free PDF parsing."""
    
    def __init__(self, debug: bool = False) -> None:
        self.in_transactions: bool = False
        self.past_header: bool = False
        self.skip_next_non_date: bool = False
        self.in_row: bool = False
        self.has_amount: bool = False
        self.is_credit: bool = False
        self.transaction: Transaction = Transaction()
        self.desc_parts: List[str] = []
        self.debug: bool = debug

    def flush_transaction(self, transactions: List[Transaction], reason: str = "") -> None:
        """Commits the currently parsed transaction into the output collector list."""
        if self.in_row and self.transaction.description:
            if self.desc_parts:
                self.transaction.description = " ".join(self.desc_parts)
            transactions.append(self.transaction)
            if self.debug:
                print(f"=== EMIT ({reason}): {self.transaction} ===", file=sys.stderr)
        
    def start_new_transaction(self, date: datetime) -> None:
        """Initializes state for parsing a new transaction record."""
        self.transaction = Transaction(date=date)
        self.in_row = True
        self.has_amount = False
        self.is_credit = False
        self.desc_parts.clear()

    def exit_section(self) -> None:
        """Resets all state flags when exiting the ledger section."""
        self.in_transactions = False
        self.transaction = Transaction()
        self.in_row = False
        self.has_amount = False
        self.desc_parts.clear()


# Regular expressions for parsing transaction boundaries
DATE_RE: Pattern[str] = re.compile(r"^(?P<date>\d{2}/\d{2}/\d{4}(?:\s*\|\s*\d{2}:\d{2}|\s+\d{2}:\d{2}:\d{2})?)(?:\s+(?P<rest>.*))?$")
MERGED_POINTS_AMOUNT_RE: Pattern[str] = re.compile(r"^([-+]?\s*\d+)\s+([-+]?[\d,]+\.\d{2}(?:\s*Cr|\s*\+)?)$", re.IGNORECASE)

HDFC_PRODUCTS: Dict[str, str] = {
    "infinia": "Infinia",
    "diners black": "Diners Black",
    "diners club": "Diners Club",
    "tata neu": "Tata Neu",
    "regalia gold": "Regalia Gold",
    "regalia": "Regalia",
    "millennia": "Millennia",
    "moneyback": "MoneyBack",
    "freedom": "Freedom",
    "swiggy": "Swiggy",
}

ICICI_PRODUCTS: Dict[str, str] = {
    "amazon pay": "Amazon Pay ICICI",
    "amazon": "Amazon Pay ICICI",
    "coral": "Coral",
    "rubyx": "Rubyx",
    "sapphiro": "Sapphiro",
    "emeralde": "Emeralde",
    "platinum": "Platinum",
    "makemytrip": "MakeMyTrip",
}


def load_card_mappings(path: Optional[str]) -> Dict[str, str]:
    """Loads card/account custom names mappings from JSON."""
    if not path:
        return {}
    if not os.path.exists(path):
        fallback = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config", os.path.basename(path))
        if os.path.exists(fallback):
            path = fallback
        else:
            return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"Warning: Could not load cards mapping from {path}: {e}", file=sys.stderr)
        return {}


def detect_card_details(
    first_page_text: str, 
    filename: str, 
    cards_mapping: Optional[Dict[str, str]] = None
) -> Dict[str, Any]:
    """
    Analyzes statement text and filename to identify issuer, account number, and card product.
    Returns a unified metadata dictionary.
    """
    if cards_mapping is None:
        cards_mapping = {}
        
    text_lower = first_page_text.lower()
    
    # 1. Detect Issuer Bank
    issuer = "Unknown"
    if "hdfc bank" in text_lower or "hdfcbank" in text_lower:
        issuer = "HDFC"
    elif "icici bank" in text_lower or "icicibank" in text_lower:
        issuer = "ICICI"
    elif "state bank of india" in text_lower or "sbi.co.in" in text_lower or "cif number" in text_lower:
        issuer = "SBI"
    else:
        fn_lower = filename.lower()
        if "hdfc" in fn_lower:
            issuer = "HDFC"
        elif "icici" in fn_lower:
            issuer = "ICICI"
        elif "sbi" in fn_lower:
            issuer = "SBI"
            
    # 2. Extract Account / Card Identifier
    card_id_from_fn = os.path.basename(filename).split('_')[0]
    
    if issuer == "Unknown" and card_id_from_fn:
        if card_id_from_fn.startswith(("4854", "0036", "6529")):
            issuer = "HDFC"
        elif card_id_from_fn.startswith("4315"):
            issuer = "ICICI"
    
    last_4 = None
    card_number = None
    
    # Regex targeting account/card sequences
    card_pattern = re.compile(
        r'(?:card\s*no(?:.|\s+)?|card\s*number|credit\s*card\s*number|account\s*number|account\s*no(?:.|\s+)?|saving\s*account\s*number|a/c\s*no(?:.|\s+)?|statement\s*for\s*card)\s*(?::|is|-|\s)?\s*([0-9X\* -]{10,22})',
        re.IGNORECASE
    )
    matches = card_pattern.findall(first_page_text)
    if matches:
        for match in matches:
            cleaned_num = re.sub(r'[^0-9X\*]', '', match)
            if len(cleaned_num) >= 10:
                card_number = cleaned_num
                last_4 = cleaned_num[-4:]
                break
                
    if not last_4:
        general_card_pat = re.compile(r'\b(?:[34560]\d{3}[ -]?[\dX\*]{4,10}[ -]?[\dX\*]{2,4}|[34560][\dX\*]{13,15})\b')
        gen_matches = general_card_pat.findall(first_page_text)
        if gen_matches:
            for m in gen_matches:
                cleaned = re.sub(r'[^0-9X\*]', '', m)
                if len(cleaned) >= 14:
                    card_number = cleaned
                    last_4 = cleaned[-4:]
                    break
                    
    if not last_4 and card_id_from_fn:
        card_number = card_id_from_fn
        last_4 = card_id_from_fn[-4:] if len(card_id_from_fn) > 4 else card_id_from_fn
            
    if not last_4:
        last_4 = "Unknown"
        
    # 3. Detect Bank Product Type
    product = "Card"
    if issuer == "HDFC":
        if "account type :" in text_lower or "statement of account" in text_lower or "cust id :" in text_lower:
            product = "Savings Account"
        else:
            for key, val in HDFC_PRODUCTS.items():
                if key in text_lower:
                    product = val
                    break
            if product == "Card" and "diners" in text_lower:
                product = "Diners Black"
    elif issuer == "ICICI":
        for key, val in ICICI_PRODUCTS.items():
            if key in text_lower:
                product = val
                break
        if product == "Card" and "amazon" in text_lower:
            product = "Amazon Pay ICICI"
    elif issuer == "SBI":
        product = "Savings Account"
        
    # 4. Resolve Card custom naming overrides
    card_name = None
    if cards_mapping:
        if card_number in cards_mapping:
            card_name = cards_mapping[card_number]
        elif card_id_from_fn in cards_mapping:
            card_name = cards_mapping[card_id_from_fn]
        elif last_4 in cards_mapping:
            card_name = cards_mapping[last_4]
            
    if not card_name:
        if issuer == "ICICI" and product == "Amazon Pay ICICI":
            card_name = f"Amazon Pay ICICI (*{last_4})"
        elif product == "Savings Account":
            card_name = f"{issuer} Savings A/C (*{last_4})"
        elif last_4 != "Unknown":
            card_name = f"{issuer} {product} (*{last_4})"
        else:
            card_name = f"{issuer} {product}"
            
    return {
        "issuer": issuer,
        "card_number": card_number,
        "last_4": last_4,
        "product": product,
        "card_name": card_name
    }


def parse_hdfc_bank(
    reader: pypdf.PdfReader, 
    password: str, 
    cardholder_name: str, 
    card_name: str, 
    debug: bool = False
) -> List[Transaction]:
    """Parses HDFC Savings/Current Account statements, reconstructing credit/debit directions."""
    opening_balance: Optional[float] = None
    
    # Find the Opening Balance
    for page in reader.pages:
        text = page.extract_text()
        if "Opening Balance" in text:
            lines = text.split('\n')
            for idx, line in enumerate(lines):
                if "Opening Balance" in line and idx + 1 < len(lines):
                    next_line = lines[idx+1].strip()
                    parts = next_line.split()
                    if parts:
                        try:
                            opening_balance = float(parts[0].replace(",", "").strip())
                            break
                        except ValueError:
                            pass
            if opening_balance is not None:
                break

    if opening_balance is None:
        if debug:
            print("Warning: Could not find HDFC bank opening balance, defaulting to 0.0", file=sys.stderr)
        opening_balance = 0.0
    else:
        if debug:
            print(f"Found HDFC bank Opening Balance: {opening_balance}", file=sys.stderr)

    raw_txs: List[Transaction] = []
    
    date_re = re.compile(r"^(?P<date>\d{2}/\d{2}/\d{2})(?:\s+(?P<rest>.*))?$")
    full_row_re = re.compile(
        r"^(?P<desc>.*?)\s+(?P<ref>\S+)\s+(?P<val_date>\d{2}/\d{2}/\d{2})\s+"
        r"(?P<amount>[-+]?[\d,]+\.\d{2})\s+(?P<balance>[-+]?[\d,]+\.\d{2})$"
    )
    amount_row_re = re.compile(
        r"^(?P<ref>\S+)\s+(?P<val_date>\d{2}/\d{2}/\d{2})\s+"
        r"(?P<amount>[-+]?[\d,]+\.\d{2})\s+(?P<balance>[-+]?[\d,]+\.\d{2})$"
    )

    terminators = [
        "Page No .:",
        "STATEMENT SUMMARY :-",
        "Statement of account",
        "Opening Balance",
        "MR. "
    ]

    for page_idx, page in enumerate(reader.pages):
        text = page.extract_text()
        lines = text.split('\n')
        
        in_transactions = False
        active_tx: Optional[Transaction] = None
        desc_parts: List[str] = []
        
        for line_idx, line in enumerate(lines):
            line = line.strip()
            if not line:
                continue
                
            if "Date Narration Chq./Ref.No. Value Dt" in line:
                in_transactions = True
                continue
                
            if not in_transactions:
                if page_idx > 0 and line_idx < 15:
                    in_transactions = True
                else:
                    continue
                    
            should_terminate = False
            for term in terminators:
                if term in line or line.startswith(term):
                    should_terminate = True
                    break
            if should_terminate:
                if active_tx:
                    active_tx.description = " ".join(desc_parts).strip()
                    raw_txs.append(active_tx)
                    active_tx = None
                    desc_parts.clear()
                in_transactions = False
                continue
                
            date_match = date_re.match(line)
            if date_match:
                if active_tx:
                    active_tx.description = " ".join(desc_parts).strip()
                    raw_txs.append(active_tx)
                    active_tx = None
                    desc_parts.clear()
                    
                dt_str = date_match.group("date")
                try:
                    dt = datetime.strptime(dt_str, "%d/%m/%y")
                except ValueError:
                    continue
                
                active_tx = Transaction(date=dt, card=card_name)
                
                rest = date_match.group("rest")
                if rest:
                    rest = rest.strip()
                    full_row_match = full_row_re.match(rest)
                    if full_row_match:
                        try:
                            active_tx.amount = float(full_row_match.group("amount").replace(",", "").strip())
                            active_tx.balance = float(full_row_match.group("balance").replace(",", "").strip())
                            desc_parts = [full_row_match.group("desc")]
                            active_tx.description = desc_parts[0]
                            raw_txs.append(active_tx)
                            active_tx = None
                            desc_parts.clear()
                        except ValueError:
                            desc_parts = [rest]
                    else:
                        desc_parts = [rest]
            else:
                if active_tx:
                    amt_match = amount_row_re.match(line)
                    if amt_match:
                        try:
                            active_tx.amount = float(amt_match.group("amount").replace(",", "").strip())
                            active_tx.balance = float(amt_match.group("balance").replace(",", "").strip())
                        except ValueError:
                            pass
                    else:
                        desc_parts.append(line)
                        
        if active_tx:
            active_tx.description = " ".join(desc_parts).strip()
            raw_txs.append(active_tx)
            active_tx = None
            desc_parts.clear()

    # Deduce positive vs negative transaction direction based on running balance
    prev_balance = opening_balance
    final_txs: List[Transaction] = []
    for tx in raw_txs:
        if tx.balance is None:
            if debug:
                print(f"Warning: Transaction missing balance, skipping sign resolution: {tx}", file=sys.stderr)
            final_txs.append(tx)
            continue
        diff = tx.balance - prev_balance
        if diff < 0:
            tx.amount = -abs(tx.amount)
        else:
            tx.amount = abs(tx.amount)
        prev_balance = tx.balance
        final_txs.append(tx)
        
    return final_txs


def parse_hdfc(
    reader: pypdf.PdfReader, 
    cardholder_name: str, 
    card_name: str, 
    debug: bool = False
) -> List[Transaction]:
    """Parses standard HDFC credit card statements (e.g. Infinia, Diners Black)."""
    transactions: List[Transaction] = []
    
    for page_idx, page in enumerate(reader.pages):
        state = ParserState(debug=debug)
        texts: List[str] = []
        
        def visitor(text: str, cm: Any, tm: Any, font_dict: Any, font_size: Any) -> None:
            t = text.strip()
            if t:
                texts.append(t)
                
        page.extract_text(visitor_text=visitor)
        
        # Normalize text segments to split merged points and amounts
        processed_texts: List[str] = []
        for text in texts:
            match = DATE_RE.match(text)
            if match:
                processed_texts.append(match.group("date"))
                rest = match.group("rest")
                if rest:
                    points_amt_match = MERGED_POINTS_AMOUNT_RE.match(rest)
                    if points_amt_match:
                        processed_texts.append(points_amt_match.group(1))
                        processed_texts.append(points_amt_match.group(2))
                    else:
                        processed_texts.append(rest)
            else:
                points_amt_match = MERGED_POINTS_AMOUNT_RE.match(text)
                if points_amt_match:
                    processed_texts.append(points_amt_match.group(1))
                    processed_texts.append(points_amt_match.group(2))
                else:
                    processed_texts.append(text)
        texts = processed_texts
        
        for idx, text in enumerate(texts):
            if text.endswith("+") and len(text) > 1:
                state.is_credit = True
                text = text[:-1].strip()
            elif (text.endswith("Cr") or text.endswith("cr") or text.endswith("CR")) and len(text) > 2:
                state.is_credit = True
                text = text[:-2].strip()

            if debug:
                print(f"{idx}: {repr(text)}", file=sys.stderr)
                
            if text in ("Domestic Transactions", "International Transactions"):
                state.in_transactions = True
                state.past_header = False
                continue
                
            if not state.in_transactions:
                continue
                
            if not state.past_header:
                if (cardholder_name.upper() in text.upper()) or text == "PI" or text.endswith(" PI") or text.endswith("PI"):
                    state.past_header = True
                    state.skip_next_non_date = True
                    if debug:
                        print(f"=== PAST HEADER (trigger: {text}) ===", file=sys.stderr)
                continue
                
            if state.skip_next_non_date:
                state.skip_next_non_date = False
                if parse_transaction_date(text) is None:
                    if debug:
                        print(f"=== SKIP CARDHOLDER NAME: {text} ===", file=sys.stderr)
                    continue
                    
            if is_section_terminator(text):
                state.flush_transaction(transactions, "section end")
                state.exit_section()
                continue
                
            dt = parse_transaction_date(text)
            if dt is not None:
                state.flush_transaction(transactions, "new date")
                state.start_new_transaction(dt)
                state.transaction.card = card_name
                continue
                
            if not state.in_row:
                continue
                
            if is_skippable_symbol(text):
                if text == "+":
                    state.is_credit = True
                elif text == "Cr":
                    state.transaction.amount = abs(state.transaction.amount)
                continue
                
            if is_page_number(text) or is_page_header(text):
                continue
                
            if is_foreign_currency(text):
                state.desc_parts.append(text)
                continue
                
            if "." in text:
                amt = parse_amount(text, state.is_credit)
                if amt is not None:
                    state.transaction.amount = amt
                    state.has_amount = True
                    state.is_credit = False
                    continue
                    
            pts = parse_points(text)
            if pts is not None:
                state.transaction.points = pts
                continue
                
            if state.has_amount:
                if debug:
                    print(f"=== SKIP POST-AMOUNT TEXT: {text} ===", file=sys.stderr)
                continue
                
            state.desc_parts.append(text)
            if not state.transaction.description:
                state.transaction.description = text
                
        state.flush_transaction(transactions, "page end")
        
    return transactions


def parse_icici(reader: pypdf.PdfReader, card_name: str, debug: bool = False) -> List[Transaction]:
    """Parses standard ICICI credit card statements (e.g. Amazon Pay ICICI)."""
    START_RE = re.compile(r"^(\d{2}/\d{2}/\d{4})\s+(\d{11})\s*(.*)$")
    END_RE = re.compile(r"^(.*?)\s*(-?\d+)\s+([\d,]+\.\d{2})(?:\s+(CR))?$")
    
    transactions: List[Transaction] = []
    current_tx: Optional[Dict[str, Any]] = None
    
    for page_idx, page in enumerate(reader.pages):
        text = page.extract_text()
        lines = text.split('\n')
        for line in lines:
            line = line.strip()
            if not line:
                continue
                
            start_match = START_RE.match(line)
            if start_match:
                if current_tx:
                    desc = " ".join(current_tx['desc_parts']).strip()
                    transactions.append(Transaction(
                        date=current_tx['date'],
                        description=desc,
                        points=current_tx['points'],
                        amount=current_tx['amount'],
                        card=current_tx['card']
                    ))
                    current_tx = None
                    
                date_str = start_match.group(1)
                rest = start_match.group(3).strip()
                date_obj = datetime.strptime(date_str, "%d/%m/%Y")
                
                end_match = END_RE.match(rest)
                if end_match:
                    desc = end_match.group(1).strip()
                    points = int(end_match.group(2))
                    amt_str = end_match.group(3).replace(',', '')
                    is_credit = end_match.group(4) == 'CR'
                    amount = float(amt_str)
                    if not is_credit:
                        amount = -amount
                    
                    transactions.append(Transaction(
                        date=date_obj,
                        description=desc,
                        points=points,
                        amount=amount,
                        card=card_name
                    ))
                else:
                    current_tx = {
                        'date': date_obj,
                        'desc_parts': [rest] if rest else [],
                        'points': 0,
                        'amount': 0.0,
                        'card': card_name
                    }
            else:
                if current_tx:
                    end_match = END_RE.match(line)
                    if end_match:
                        desc_part = end_match.group(1).strip()
                        if desc_part:
                            current_tx['desc_parts'].append(desc_part)
                        points = int(end_match.group(2))
                        amt_str = end_match.group(3).replace(',', '')
                        is_credit = end_match.group(4) == 'CR'
                        amount = float(amt_str)
                        if not is_credit:
                            amount = -amount
                            
                        desc = " ".join(current_tx['desc_parts']).strip()
                        transactions.append(Transaction(
                            date=current_tx['date'],
                            description=desc,
                            points=points,
                            amount=amount,
                            card=current_tx['card']
                        ))
                        current_tx = None
                    else:
                        current_tx['desc_parts'].append(line)
                        
    if current_tx:
        desc = " ".join(current_tx['desc_parts']).strip()
        transactions.append(Transaction(
            date=current_tx['date'],
            description=desc,
            points=current_tx['points'],
            amount=current_tx['amount'],
            card=current_tx['card']
        ))
        
    return transactions


def resolve_sbi_card_name(acc_num: str, cards_mapping: Optional[Dict[str, str]] = None) -> str:
    """Resolves a custom name override for an SBI account number."""
    last_4 = acc_num[-4:] if len(acc_num) >= 4 else acc_num
    card_name = None
    if cards_mapping:
        if acc_num in cards_mapping:
            card_name = cards_mapping[acc_num]
        elif last_4 in cards_mapping:
            card_name = cards_mapping[last_4]
    if not card_name:
        card_name = f"SBI Savings A/C (*{last_4})"
    return card_name


def parse_sbi_monthly_statements(
    reader: pypdf.PdfReader, 
    cards_mapping: Optional[Dict[str, str]] = None, 
    debug: bool = False
) -> List[Transaction]:
    """Parses SBI Format A statements (standard monthly statements)."""
    transactions: List[Transaction] = []
    current_account = "SBI Account"
    DATE_RE_SBI = re.compile(r"^\d{2}-\d{2}-\d{2}$")
    
    for page_idx, page in enumerate(reader.pages):
        text = page.extract_text()
        lines = text.split('\n')
        
        i = 0
        while i < len(lines):
            line = lines[i].strip()
            if not line:
                i += 1
                continue
                
            if line == "TRANSACTION DETAILS":
                if i + 2 < len(lines) and lines[i+1].strip() == "SAVING ACCOUNT":
                    acc_num = lines[i+2].strip()
                    current_account = resolve_sbi_card_name(acc_num, cards_mapping)
                    if debug:
                        print(f"Found SBI account section: {current_account} on Page {page_idx+1}", file=sys.stderr)
                    i += 3
                    continue
            
            parts = line.split(None, 1)
            if parts and len(parts) == 2:
                date_part = parts[0]
                rest = parts[1]
                
                if DATE_RE_SBI.match(date_part):
                    sub_parts = rest.rsplit(None, 3)
                    if len(sub_parts) == 4:
                        desc_part, credit_str, debit_str, balance_str = sub_parts
                        try:
                            credit_val = float(credit_str.replace(',', '')) if credit_str != '0' else 0.0
                            debit_val = float(debit_str.replace(',', '')) if debit_str != '0' else 0.0
                            
                            amount = credit_val if credit_val > 0 else -debit_val
                            dt = datetime.strptime(date_part, "%d-%m-%y")
                            
                            tx = Transaction(
                                date=dt,
                                description=desc_part,
                                points=0,
                                amount=amount,
                                card=current_account
                            )
                            transactions.append(tx)
                            if debug:
                                print(f"SBI Parsed: {tx}", file=sys.stderr)
                        except ValueError as ve:
                            if debug:
                                print(f"Failed to parse SBI amounts/date on line: {line}. Err: {ve}", file=sys.stderr)
            i += 1
    return transactions


def parse_sbi_custom_statements(
    reader: pypdf.PdfReader, 
    cards_mapping: Optional[Dict[str, str]] = None, 
    debug: bool = False
) -> List[Transaction]:
    """Parses SBI Format B statements (custom date-range statement downloads)."""
    transactions: List[Transaction] = []
    current_account = "SBI Account"
    
    DOUBLE_DATE_RE = re.compile(r"^(\d{2}/\d{2}/\d{4})\s+(\d{2}/\d{2}/\d{4})(?:\s+(.*))?$")
    AMOUNT_LINE_RE = re.compile(r"^(\S+)\s+(\S+)\s+(\S+)\s+([\d,]+\.\d{2})$")
    
    active_tx: Optional[Transaction] = None
    desc_parts: List[str] = []
    
    for page_idx, page in enumerate(reader.pages):
        text = page.extract_text()
        lines = text.split('\n')
        
        if page_idx == 1: # Read Account Number from Page 2
            for i, line in enumerate(lines):
                if ":Account Number" in line:
                    if i + 7 < len(lines):
                        acc_num = lines[i+7].strip()
                        current_account = resolve_sbi_card_name(acc_num, cards_mapping)
                        if debug:
                            print(f"Found SBI account: {current_account}", file=sys.stderr)
                        break
                        
        i = 0
        while i < len(lines):
            line = lines[i].strip()
            if not line:
                i += 1
                continue
                
            if "STATEMENT OF ACCOUNT" in line or "State Bank of India" in line or "Page no." in line or "Statement Summary" in line:
                i += 1
                continue
                
            double_date_match = DOUBLE_DATE_RE.match(line)
            if double_date_match:
                if active_tx:
                    active_tx.description = " ".join(desc_parts).strip()
                    transactions.append(active_tx)
                    active_tx = None
                    desc_parts.clear()
                    
                date_str = double_date_match.group(1)
                dt = datetime.strptime(date_str, "%d/%m/%Y")
                rest = double_date_match.group(3)
                
                active_tx = Transaction(
                    date=dt,
                    card=current_account,
                    points=0
                )
                desc_parts.clear()
                
                if rest:
                    sub_parts = rest.rsplit(None, 4)
                    if len(sub_parts) == 5 and AMOUNT_LINE_RE.match(" ".join(sub_parts[1:])):
                        desc_part = sub_parts[0]
                        cheque, debit_str, credit_str, balance_str = sub_parts[1:]
                        
                        credit_val = float(credit_str.replace(',', '')) if credit_str != '-' else 0.0
                        debit_val = float(debit_str.replace(',', '')) if debit_str != '-' else 0.0
                        
                        active_tx.amount = credit_val if credit_val > 0 else -debit_val
                        active_tx.description = desc_part
                        transactions.append(active_tx)
                        if debug:
                            print(f"SBI Parsed (Single Line): {active_tx}", file=sys.stderr)
                        active_tx = None
                    else:
                        desc_parts.append(rest)
                i += 1
                continue
                
            if active_tx:
                amount_match = AMOUNT_LINE_RE.match(line)
                if amount_match:
                    cheque, debit_str, credit_str, balance_str = amount_match.groups()
                    if (debit_str == '-' or '.' in debit_str) and (credit_str == '-' or '.' in credit_str):
                        credit_val = float(credit_str.replace(',', '')) if credit_str != '-' else 0.0
                        debit_val = float(debit_str.replace(',', '')) if debit_str != '-' else 0.0
                        
                        active_tx.amount = credit_val if credit_val > 0 else -debit_val
                        active_tx.description = " ".join(desc_parts).strip()
                        transactions.append(active_tx)
                        if debug:
                            print(f"SBI Parsed (Multi Line): {active_tx}", file=sys.stderr)
                        active_tx = None
                        desc_parts.clear()
                    else:
                        desc_parts.append(line)
                else:
                    desc_parts.append(line)
            i += 1
            
    return transactions


def parse_sbi(
    reader: pypdf.PdfReader, 
    cards_mapping: Optional[Dict[str, str]] = None, 
    debug: bool = False
) -> List[Transaction]:
    """Orchestrates and auto-detects which SBI parser to run on the statement."""
    first_page_text = ""
    if len(reader.pages) > 0:
        first_page_text = reader.pages[0].extract_text()
        
    is_format_b = "relationship summary" in first_page_text.lower() or "cif number" in first_page_text.lower()
    
    if is_format_b:
        if debug:
            print("Detected SBI Custom Statement format (Format B)", file=sys.stderr)
        return parse_sbi_custom_statements(reader, cards_mapping=cards_mapping, debug=debug)
    else:
        if debug:
            print("Detected SBI Monthly Statement format (Format A)", file=sys.stderr)
        return parse_sbi_monthly_statements(reader, cards_mapping=cards_mapping, debug=debug)


def parse_pdf(
    path: str, 
    cardholder_name: str, 
    password: str, 
    cards_mapping: Optional[Dict[str, str]] = None, 
    debug: bool = False
) -> List[Transaction]:
    """Main interface to decrypt, recognize issuer, and extract PDF bank or credit card statement."""
    reader = pypdf.PdfReader(path)
    if reader.is_encrypted:
        decrypted = False
        
        # Safe parsing of multi-passwords list
        password_str = str(password or "")
        passwords = [p.strip() for p in re.split(r'[,;]', password_str) if p.strip()]
        if not passwords:
            passwords = [""]
            
        for base_pwd in passwords:
            for pwd in [base_pwd, base_pwd.lower(), base_pwd.upper()]:
                try:
                    if reader.decrypt(pwd):
                        decrypted = True
                        break
                except Exception:
                    pass
            if decrypted:
                break
                
        if not decrypted:
            raise ValueError(f"Failed to decrypt PDF statement: {os.path.basename(path)}")
            
    first_page_text = ""
    if len(reader.pages) > 0:
        first_page_text = reader.pages[0].extract_text()
        
    # Detect issuer bank parameters
    card_details = detect_card_details(first_page_text, path, cards_mapping)
    card_name = card_details["card_name"]
    issuer = card_details["issuer"]
            
    is_sbi = issuer == "SBI"
    is_icici = issuer == "ICICI"
    
    if is_sbi:
        if debug:
            print(f"Detected SBI statement format for {os.path.basename(path)}", file=sys.stderr)
        txs = parse_sbi(reader, cards_mapping=cards_mapping, debug=debug)
        for tx in txs:
            tx.account_type = "bank"
        return txs
    elif is_icici:
        if debug:
            print(f"Detected ICICI statement format for {card_name}", file=sys.stderr)
        txs = parse_icici(reader, card_name, debug=debug)
        for tx in txs:
            tx.account_type = "credit"
        return txs
    elif issuer == "HDFC" and card_details.get("product") == "Savings Account":
        if debug:
            print(f"Detected HDFC bank statement format for {card_name}", file=sys.stderr)
        txs = parse_hdfc_bank(reader, password, cardholder_name, card_name, debug=debug)
        for tx in txs:
            tx.account_type = "bank"
        return txs
    else:
        if debug:
            print(f"Detected HDFC statement format for {card_name}", file=sys.stderr)
        txs = parse_hdfc(reader, cardholder_name, card_name, debug=debug)
        for tx in txs:
            tx.account_type = "credit"
        return txs


def date_format_to_regex(date_format: str) -> Pattern[str]:
    """Translates datetime string format tokens to compiled regular expressions."""
    regex_str = (
        date_format.replace("%Y", r"\d{4}")
        .replace("%m", r"\d{2}")
        .replace("%d", r"\d{2}")
        .replace("%H", r"\d{2}")
        .replace("%M", r"\d{2}")
        .replace("%S", r"\d{2}")
        .replace("%z", r"[\+\-]\d{4}")
        .replace("%Z", r"[A-Z]{3}")
    )
    return re.compile(regex_str)


def extract_date_from_filename(filename: str, format_str: str, regex: Pattern[str]) -> datetime.date:
    """Attempts to extract a sorting date from a PDF filename using regex."""
    match = regex.search(filename)
    if match:
        try:
            return datetime.strptime(match.group(0), format_str).date()
        except ValueError:
            pass
    return datetime(1970, 1, 1).date()


def sort_files_by_date(files: List[str], date_format: str) -> None:
    """Sorts in-place a list of filepaths by the dates embedded in their filenames."""
    regex = date_format_to_regex(date_format)
    files.sort(key=lambda f: extract_date_from_filename(os.path.basename(f), date_format, regex))


DEFAULT_PAYMENT_PATTERNS: List[str] = [
    "CREDIT CARD PAYMENT",
    "NETBANKING TRANSFER",
    "AUTOPAY",
    "BBPS PAYMENT",
    "TELE TRANSFER CREDIT",
    "CC PAYMENT"
]


def is_bill_payment(description: str, payment_patterns: Optional[List[str]] = None) -> bool:
    """Flags if a transaction is a credit card bill payment or refund transfer."""
    if payment_patterns is None:
        payment_patterns = DEFAULT_PAYMENT_PATTERNS
    desc_upper = description.upper()
    return any(pattern.upper() in desc_upper for pattern in payment_patterns)


def load_categories(path: str) -> Dict[str, List[str]]:
    """Loads expense category definitions from a JSON configuration."""
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def categorize(description: str, categories: Dict[str, List[str]]) -> Optional[str]:
    """Resolves transaction category case-insensitively using keyword mappings."""
    desc_upper = description.upper()
    for category, patterns in categories.items():
        for pattern in patterns:
            if pattern.upper() in desc_upper:
                return category
    return None


def print_summary(transactions: List[Transaction], categories: Optional[Dict[str, List[str]]] = None) -> None:
    """Prints structured transaction summary and category distribution in the CLI."""
    total_spent = 0.0
    bill_payment = 0.0
    total_points = 0
    category_totals: Dict[str, float] = {}
    uncategorized = 0.0
    transaction_count = 0
    
    payment_patterns = None
    if categories:
        payment_patterns = categories.get("Payment") or categories.get("__payments__")

    for tx in transactions:
        transaction_count += 1
        total_points += tx.points
        
        if is_bill_payment(tx.description, payment_patterns):
            bill_payment += tx.amount
        elif tx.amount < 0.0:
            spent = abs(tx.amount)
            total_spent += spent
            
            if categories:
                cat = categorize(tx.description, categories)
                if cat:
                    category_totals[cat] = category_totals.get(cat, 0.0) + spent
                else:
                    uncategorized += spent
                    
    print()
    print("═══════════════════════════════════════════")
    print("              SUMMARY")
    print("═══════════════════════════════════════════")
    print(f"Total Spent:          ₹ {total_spent:>12.2f}")
    print(f"Bill Payment:         ₹ {bill_payment:>12.2f}")
    print(f"Points Earned:        {total_points:>15d}")
    print(f"Transactions:         {transaction_count:>15d}")
    
    if categories and (category_totals or uncategorized > 0):
        print()
        print("───────────────────────────────────────────")
        print("         CATEGORY BREAKDOWN")
        print("───────────────────────────────────────────")
        
        sorted_categories = sorted(category_totals.items(), key=lambda x: x[1], reverse=True)
        for category, amount in sorted_categories:
            percentage = (amount / total_spent * 100.0) if total_spent > 0.0 else 0.0
            print(f"{category:<20}  ₹ {amount:>10.2f}  ({percentage:>5.1f}%)")
            
        if uncategorized > 0.0:
            percentage = (uncategorized / total_spent * 100.0) if total_spent > 0.0 else 0.0
            print(f"{'Uncategorized':<20}  ₹ {uncategorized:>10.2f}  ({percentage:>5.1f}%)")
            
        print("───────────────────────────────────────────")
    print()


def write_csv(transactions: List[Transaction], add_headers: bool = False) -> None:
    """Writes standardized transactions list in CSV format to stdout."""
    writer = csv.writer(sys.stdout)
    if add_headers:
        writer.writerow(["Date", "Description", "Points", "Amount", "Card"])
    for tx in transactions:
        dt_str = tx.date.strftime("%Y-%m-%d %H:%M:%S")
        writer.writerow([
            dt_str,
            tx.description,
            str(tx.points),
            str(tx.amount),
            tx.card
        ])


def main() -> None:
    """CLI Entry point for raw parsing exports."""
    parser = argparse.ArgumentParser(description="Financial Statement PDF parser engine")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dir", help="Path to directory containing PDF statements")
    group.add_argument("--file", help="Path to a single PDF statement")
    
    parser.add_argument("--name", required=True, help="Cardholder name as it appears on the statement")
    parser.add_argument("--password", default="", help="PDF password (if encrypted)")
    parser.add_argument("--sortformat", help="Date format in filenames for sorting (e.g., %%d-%%m-%%Y)")
    parser.add_argument("--addheaders", action="store_true", help="Add CSV header row to output")
    parser.add_argument("--summary", action="store_true", help="Show summary only (no CSV output)")
    parser.add_argument("--categories", help="JSON file for category breakdown (requires --summary)")
    parser.add_argument("--cards", help="JSON file for custom card mappings")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging to stderr")
    
    args = parser.parse_args()
    
    pdf_files: List[str] = []
    if args.dir:
        if not os.path.isdir(args.dir):
            print(f"Error opening statements directory: {args.dir}", file=sys.stderr)
            sys.exit(1)
        for entry in os.scandir(args.dir):
            if entry.is_file() and entry.name.lower().endswith(".pdf"):
                pdf_files.append(entry.path)
    elif args.file:
        if not os.path.exists(args.file):
            print(f"Error opening statement file: {args.file}", file=sys.stderr)
            sys.exit(1)
        pdf_files.append(args.file)
        
    if args.sortformat and pdf_files:
        sort_files_by_date(pdf_files, args.sortformat)
        
    categories = None
    if args.categories:
        try:
            categories = load_categories(args.categories)
        except Exception as e:
            print(f"Error loading categories: {e}", file=sys.stderr)
            sys.exit(1)
            
    cards_mapping = None
    if args.cards:
        try:
            cards_mapping = load_card_mappings(args.cards)
        except Exception as e:
            print(f"Error loading card mappings: {e}", file=sys.stderr)
            sys.exit(1)
            
    all_transactions: List[Transaction] = []
    for file in pdf_files:
        try:
            txs = parse_pdf(file, args.name, args.password, cards_mapping=cards_mapping, debug=args.debug)
            all_transactions.extend(txs)
        except Exception as e:
            print(f"Failed to parse statement {file}: {e}", file=sys.stderr)
            sys.exit(1)
            
    if args.summary:
        print_summary(all_transactions, categories)
    else:
        write_csv(all_transactions, args.addheaders)


if __name__ == "__main__":
    main()
