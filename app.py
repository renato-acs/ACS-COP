import streamlit as st
import pandas as pd
import gspread
from google.oauth2.service_account import Credentials
from google.oauth2 import service_account
import requests
import time
import base64
from io import BytesIO
from pypdf import PdfWriter, PdfReader, Transformation
import json

# --- CONFIGURATION ---
# IDs for your Google Sheets
# NOTE: We use the SOURCE_SHEET_ID as our "Database" where we dump CSV data
SOURCE_SHEET_ID = "1nb8gE9i3GmxquG93hLX0a5Kn_GoGH1uCESdVxtXnkv0"
LABEL_TEMPLATE_ID = "1fUuCsIumgRAmJEt-FvvaXrjDaVTT6FJtGz162ZYIwLY"
PACKING_SLIP_ID = "1fr-Mjq0rkQadr-5nvaOK5YqyCo5Teye_wS4-P1UN7po"

# Mapping: CSV Header -> Internal Variable Name
CSV_MAP = {
    "Order Number": "order_num",
    "PO Number": "po_num",
    "CustomerName": "customer_name",
    "Item": "vendor_sku",
    "ItemDescription": "description",
    "OrderedQty": "ordered_qty",
    "Ship To Address 1": "address_1",
    "Ship To Address 2": "address_2",
    "Cust SKU": "customer_sku",
    "Match Data for Address": "city_state_zip",
}

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# --- USERS (Simple Auth) ---
# Format: username: (password, role)
USERS = {
    "admin": ("admin123", "manager"),       # YOU (Can upload files)
    "warehouse": ("packer2024", "packer")   # STAFF (Can only print)
}

# --- AUTHENTICATION & SETUP ---
@st.cache_resource
def get_gspread_client():
    try:
        # Try loading from Streamlit Secrets (Production)
        if "gcp_service_account" in st.secrets:
            # Create a dictionary from the secrets object
            creds_dict = dict(st.secrets["gcp_service_account"])
            # Fix potential newline issues in private_key if copied manually
            creds_dict["private_key"] = creds_dict["private_key"].replace("\\n", "\n")
            
            creds = service_account.Credentials.from_service_account_info(
                creds_dict, scopes=SCOPES
            )
        else:
            # Fallback to local file (Development)
            creds = Credentials.from_service_account_file("credentials.json", scopes=SCOPES)
            
        client = gspread.authorize(creds)
        return client, creds
    except Exception as e:
        st.error(f"Authentication Error: {e}")
        return None, None

# --- HELPER FUNCTIONS ---
def truncate_text(text, max_len=55):
    text = str(text) if text else ""
    if len(text) > max_len:
        return text[:max_len] + "..."
    return text

def export_sheet_to_pdf(sheet_id, sheet_gid, creds, margin=0, fit=True):
    url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export"
    params = {
        "format": "pdf",
        "gid": sheet_gid,
        "portrait": "true",
        "fitw": "true" if fit else "false",
        "gridlines": "false",
        "top_margin": str(margin),
        "bottom_margin": str(margin),
        "left_margin": str(margin),
        "right_margin": str(margin),
    }
    headers = {"Authorization": f"Bearer {creds.token}"}
    if not creds.valid:
        from google.auth.transport.requests import Request
        creds.refresh(Request())
        headers = {"Authorization": f"Bearer {creds.token}"}

    response = requests.get(url, params=params, headers=headers)
    if response.status_code == 200:
        return response.content
    return None

def show_pdf(pdf_bytes, height=500):
    base64_pdf = base64.b64encode(pdf_bytes).decode('utf-8')
    pdf_display = f'<iframe src="data:application/pdf;base64,{base64_pdf}" width="100%" height="{height}" type="application/pdf"></iframe>'
    st.markdown(pdf_display, unsafe_allow_html=True)

# --- APP LOGIC ---

def login_screen():
    st.markdown("## 🔐 Warehouse Portal Login")
    with st.form("login_form"):
        username = st.text_input("Username")
        password = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Sign In")
        
        if submitted:
            if username in USERS and USERS[username][0] == password:
                st.session_state["logged_in"] = True
                st.session_state["username"] = username
                st.session_state["role"] = USERS[username][1]
                st.rerun()
            else:
                st.error("Invalid username or password")

def admin_interface(client):
    st.title("🛠️ Manager Dashboard")
    st.markdown("### 📤 Upload Source Data")
    st.info("Upload your CSV files here. This will OVERWRITE the current active orders database.")
    
    uploaded_files = st.file_uploader("Choose CSV files", accept_multiple_files=True, type="csv")
    
    if st.button("🚀 Process & Update Database", type="primary"):
        if uploaded_files:
            with st.spinner("Reading CSVs and Updating Google Sheet Database..."):
                try:
                    all_dfs = []
                    for uploaded_file in uploaded_files:
                        # Read CSV
                        df = pd.read_csv(uploaded_file)
                        
                        # Normalize headers (strip whitespace, etc)
                        df.columns = df.columns.str.strip()
                        
                        # Rename columns based on mapping
                        # Filter to only include mapped columns to keep sheet clean
                        rename_map = {k: v for k, v in CSV_MAP.items() if k in df.columns}
                        df = df.rename(columns=rename_map)
                        
                        # Keep only relevant columns if they exist
                        valid_cols = [v for k, v in CSV_MAP.items() if v in df.columns]
                        df = df[valid_cols]
                        
                        all_dfs.append(df)
                    
                    if all_dfs:
                        # Combine all CSVs
                        final_df = pd.concat(all_dfs, ignore_index=True)
                        final_df = final_df.fillna("") # Replace NaNs with empty string
                        
                        # Connect to Sheet
                        sh = client.open_by_key(SOURCE_SHEET_ID)
                        ws = sh.worksheet("Open SO") # Assuming we reuse this tab name
                        
                        # CLEAR and UPDATE
                        ws.clear()
                        # Update headers
                        ws.update(range_name="A1", values=[final_df.columns.values.tolist()])
                        # Update data
                        ws.update(range_name="A2", values=final_df.values.tolist())
                        
                        st.success(f"Successfully uploaded {len(final_df)} order lines to the database!")
                        time.sleep(2)
                        st.rerun()
                except Exception as e:
                    st.error(f"Error processing files: {e}")
        else:
            st.warning("Please upload at least one CSV file.")

def warehouse_interface(client, creds):
    st.title("📦 Warehouse Control Center")
    
    # Load Data from the Google Sheet (which acts as our DB)
    @st.cache_data(ttl=60) # Cache for 60 seconds so it feels fast but updates
    def load_data_from_sheet():
        try:
            sh = client.open_by_key(SOURCE_SHEET_ID)
            ws = sh.worksheet("Open SO")
            data = ws.get_all_records()
            return pd.DataFrame(data)
        except Exception as e:
            st.error(f"Error loading data: {e}")
            return pd.DataFrame()

    if st.button("🔄 Refresh Orders"):
        st.cache_data.clear()
        st.rerun()

    df = load_data_from_sheet()

    if df.empty:
        st.warning("No active orders in the database. Please ask a Manager to upload data.")
        return

    # Ensure quantities are numeric
    if 'ordered_qty' in df.columns:
        df['ordered_qty'] = pd.to_numeric(df['ordered_qty'], errors='coerce').fillna(0)

    # --- VIEW 1: DASHBOARD ---
    if "selected_order" not in st.session_state:
        st.session_state.selected_order = None

    if st.session_state.selected_order is None:
        # Search Box
        unique_orders = df[['order_num', 'po_num', 'customer_name']].drop_duplicates()
        search_list = unique_orders.apply(
            lambda x: f"{x['order_num']} (PO: {x['po_num']}) - {x['customer_name']}", axis=1
        ).tolist()
        
        c1, c2 = st.columns([2, 1])
        with c1:
            selected_search = st.selectbox("🔍 Search Orders", options=[""] + search_list)
        
        if selected_search:
            st.session_state.selected_order = selected_search.split(" ")[0]
            st.rerun()

        # Grouped View Table
        st.markdown("### Open Orders")
        grouped_view = df.groupby(['order_num', 'po_num', 'customer_name']).agg({
            'vendor_sku': 'count',
            'ordered_qty': 'sum'
        }).reset_index()
        grouped_view.columns = ['Order #', 'PO #', 'Customer', 'Items', 'Qty']
        
        st.dataframe(grouped_view, use_container_width=True, hide_index=True)

    # --- VIEW 2: ORDER DETAILS ---
    else:
        order_id = st.session_state.selected_order
        order_data = df[df['order_num'].astype(str) == str(order_id)].copy()
        
        if order_data.empty:
            st.error("Order data missing.")
            if st.button("Back"):
                st.session_state.selected_order = None
                st.rerun()
            return
            
        header = order_data.iloc[0]

        # Header
        with st.container():
            c1, c2, c3 = st.columns([0.5, 3, 1])
            with c1:
                if st.button("⬅️ Back"):
                    st.session_state.selected_order = None
                    st.rerun()
            with c2:
                st.markdown(f"## {header['customer_name']}")
                st.caption(f"Order: {header['order_num']} | PO: {header['po_num']}")
            with c3:
                st.metric("Items", len(order_data))

        st.divider()

        # Workspace
        left_col, right_col = st.columns([1.5, 1])

        # Editable Grid
        with left_col:
            st.subheader("Items")
            display_df = order_data[['vendor_sku', 'description', 'ordered_qty', 'customer_sku']].copy()
            display_df['shipped_qty'] = display_df['ordered_qty'] # Init shipping qty
            
            edited_df = st.data_editor(
                display_df,
                column_config={
                    "shipped_qty": st.column_config.NumberColumn("Ship Qty", min_value=0),
                    "ordered_qty": st.column_config.NumberColumn("Ord", disabled=True),
                    "vendor_sku": st.column_config.TextColumn("SKU", disabled=True),
                    "description": st.column_config.TextColumn("Desc", disabled=True),
                },
                use_container_width=True,
                num_rows="fixed",
                key=f"editor_{order_id}"
            )

        # Actions
        with right_col:
            tab1, tab2 = st.tabs(["🏷️ Labels", "📄 Packing Slip"])
            
            # LABELS
            with tab1:
                item_to_print = st.selectbox("Select SKU:", edited_df['vendor_sku'].tolist())
                if item_to_print:
                    item_row = order_data[order_data['vendor_sku'] == item_to_print].iloc[0]
                    c_l1, c_l2 = st.columns(2)
                    with c_l1:
                        lbl_qty = st.number_input("Qty/Box", value=int(item_row.get('ordered_qty', 1)))
                    with c_l2:
                        lbl_count = st.number_input("# Boxes", value=1, min_value=1)
                    
                    with st.expander("Printer Settings"):
                        rotate = st.checkbox("Rotate 90°", value=True)
                        scale = st.slider("Scale", 0.5, 1.2, 0.95, 0.05)
                        off_x = st.slider("Up/Down", -100, 100, -20, 5)
                        off_y = st.slider("L/R", -100, 100, 0, 5)
                    
                    if st.button("Print Labels", type="primary"):
                        with st.spinner("Generating..."):
                            try:
                                lbl_sh = client.open_by_key(LABEL_TEMPLATE_ID)
                                lbl_ws = lbl_sh.worksheet("ItemLabel")
                                merger = PdfWriter()
                                
                                for i in range(1, int(lbl_count) + 1):
                                    desc_clean = truncate_text(item_row.get('description', ''), 55)
                                    updates = [
                                        {'range': 'C3', 'values': [[str(item_row.get('customer_sku', ''))]]},
                                        {'range': 'B5', 'values': [[desc_clean]]},
                                        {'range': 'B7', 'values': [[str(item_row.get('vendor_sku', ''))]]},
                                        {'range': 'C11', 'values': [[str(item_row.get('po_num', ''))]]},
                                        {'range': 'F7', 'values': [[lbl_qty]]},
                                        {'range': 'B9', 'values': [[i]]},
                                        {'range': 'D9', 'values': [[lbl_count]]}
                                    ]
                                    lbl_ws.batch_update(updates)
                                    time.sleep(0.5)
                                    pdf_raw = export_sheet_to_pdf(LABEL_TEMPLATE_ID, lbl_ws.id, creds, fit=False)
                                    
                                    if pdf_raw:
                                        page = PdfReader(BytesIO(pdf_raw)).pages[0]
                                        op = Transformation().scale(sx=scale, sy=scale).translate(tx=off_x, ty=off_y)
                                        page.add_transformation(op)
                                        page.mediabox.lower_left = (0, page.mediabox.top - 288) # 4 inches
                                        page.mediabox.upper_right = (432, page.mediabox.top) # 6 inches
                                        if rotate: page.rotate(90)
                                        merger.add_page(page)
                                
                                out = BytesIO()
                                merger.write(out)
                                st.download_button("Download PDF", out.getvalue(), f"Labels_{item_to_print}.pdf")
                                show_pdf(out.getvalue(), height=300)
                            except Exception as e:
                                st.error(str(e))

            # PACKING SLIP
            with tab2:
                method = st.radio("Method", ["Small Parcel", "LTL"], horizontal=True)
                if st.button("Print Slip", type="primary"):
                    with st.spinner("Generating..."):
                        try:
                            ps_sh = client.open_by_key(PACKING_SLIP_ID)
                            ps_ws = ps_sh.worksheet("Template")
                            
                            updates = [
                                {'range': 'B11', 'values': [[str(header.get('customer_name', ''))]]},
                                {'range': 'B12', 'values': [[str(header.get('address_1', ''))]]},
                                {'range': 'B13', 'values': [[str(header.get('address_2', ''))]]},
                                {'range': 'B14', 'values': [[str(header.get('city_state_zip', ''))]]},
                                {'range': 'H11', 'values': [[str(header.get('order_num', ''))]]},
                                {'range': 'H12', 'values': [[str(header.get('po_num', ''))]]},
                                {'range': 'H13', 'values': [[method]]},
                            ]
                            ps_ws.batch_update(updates)
                            
                            final_items = edited_df[edited_df['shipped_qty'] > 0]
                            ps_ws.batch_clear(["B19:H100"])
                            if not final_items.empty:
                                rows = []
                                for _, row in final_items.iterrows():
                                    rows.append([
                                        str(row['customer_sku']),
                                        str(row['vendor_sku']),
                                        int(row['ordered_qty']),
                                        int(row['shipped_qty']),
                                        truncate_text(row['description'], 55)
                                    ])
                                ps_ws.update(range_name="B19", values=rows)
                                
                            pdf_bytes = export_sheet_to_pdf(PACKING_SLIP_ID, ps_ws.id, creds)
                            if pdf_bytes:
                                st.download_button("Download Slip", pdf_bytes, f"PS_{order_id}.pdf")
                                show_pdf(pdf_bytes)
                        except Exception as e:
                            st.error(str(e))

# --- MAIN ENTRY POINT ---
st.set_page_config(page_title="Warehouse Portal", layout="wide", page_icon="🏭")

# Initialize Session State
if "logged_in" not in st.session_state:
    st.session_state["logged_in"] = False
    st.session_state["role"] = None

client, creds = get_gspread_client()

if not client:
    st.error("Could not connect to Google Services. Check Secrets/Credentials.")
else:
    if not st.session_state["logged_in"]:
        login_screen()
    else:
        # Logout Button in Sidebar
        with st.sidebar:
            st.write(f"User: **{st.session_state['username']}**")
            if st.button("Log Out"):
                st.session_state["logged_in"] = False
                st.session_state["role"] = None
                st.rerun()

        # Role Based Routing
        if st.session_state["role"] == "manager":
            # Manager sees both tabs
            page = st.sidebar.radio("Navigate", ["Upload Data (Manager)", "Warehouse View"])
            if page == "Upload Data (Manager)":
                admin_interface(client)
            else:
                warehouse_interface(client, creds)
        else:
            # Staff only sees Warehouse View
            warehouse_interface(client, creds)
