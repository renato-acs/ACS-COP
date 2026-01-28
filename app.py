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

# --- CONFIGURATION ---
SOURCE_SHEET_ID = "1nb8gE9i3GmxquG93hLX0a5Kn_GoGH1uCESdVxtXnkv0"
LABEL_TEMPLATE_ID = "1fUuCsIumgRAmJEt-FvvaXrjDaVTT6FJtGz162ZYIwLY"
PACKING_SLIP_ID = "1fr-Mjq0rkQadr-5nvaOK5YqyCo5Teye_wS4-P1UN7po"

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

SCOPES = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]

USERS = {
    "admin": ("admin123", "manager"),
    "warehouse": ("packer2024", "packer")
}

# --- SETUP ---
st.set_page_config(page_title="Warehouse Portal", layout="wide", page_icon="📦")

# Custom CSS for cleaner look (Hide Index, tight layout)
st.markdown("""
<style>
    .block-container {padding-top: 1rem;}
    button[kind="primary"] {width: 100%;}
    div[data-testid="stMetricValue"] {font-size: 1.1rem;}
</style>
""", unsafe_allow_html=True)

# --- AUTH ---
@st.cache_resource
def get_gspread_client():
    try:
        if "gcp_service_account" in st.secrets:
            creds_dict = dict(st.secrets["gcp_service_account"])
            creds_dict["private_key"] = creds_dict["private_key"].replace("\\n", "\n")
            creds = service_account.Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
        else:
            creds = Credentials.from_service_account_file("credentials.json", scopes=SCOPES)
        return gspread.authorize(creds), creds
    except Exception as e:
        st.error(f"Auth Error: {e}")
        return None, None

# --- PDF ENGINE ---
def truncate_text(text, max_len=55):
    text = str(text) if text else ""
    return text[:max_len] + "..." if len(text) > max_len else text

def export_sheet_to_pdf(sheet_id, sheet_gid, creds, fit=True):
    url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export"
    params = {
        "format": "pdf", "gid": sheet_gid, "portrait": "true",
        "fitw": "true" if fit else "false", "gridlines": "false"
    }
    headers = {"Authorization": f"Bearer {creds.token}"}
    if not creds.valid:
        from google.auth.transport.requests import Request
        creds.refresh(Request())
        headers = {"Authorization": f"Bearer {creds.token}"}
    
    resp = requests.get(url, params=params, headers=headers)
    return resp.content if resp.status_code == 200 else None

def show_pdf_preview(pdf_bytes):
    """Shows a preview, but we rely on Download Button for the file."""
    base64_pdf = base64.b64encode(pdf_bytes).decode('utf-8')
    pdf_display = f'<iframe src="data:application/pdf;base64,{base64_pdf}" width="100%" height="400" type="application/pdf"></iframe>'
    st.markdown(pdf_display, unsafe_allow_html=True)

# --- HEADER / NAV ---
def top_bar(title):
    c1, c2, c3 = st.columns([0.2, 0.6, 0.2])
    with c1:
        st.image("https://cdn-icons-png.flaticon.com/512/2821/2821898.png", width=40) # Simple Icon
    with c2:
        st.markdown(f"### {title}")
    with c3:
        if st.session_state.get("logged_in"):
            cols = st.columns([2, 1])
            with cols[0]:
                st.write(f"👤 **{st.session_state['username']}**")
            with cols[1]:
                if st.button("Logout", key="logout_btn", use_container_width=True):
                    st.session_state["logged_in"] = False
                    st.rerun()
    st.divider()

# --- LOGIC ---

def generate_single_label_pdf(item_row, label_qty, creds, client, settings):
    """Helper to generate ONE label PDF (to be merged later)."""
    lbl_sh = client.open_by_key(LABEL_TEMPLATE_ID)
    lbl_ws = lbl_sh.worksheet("ItemLabel")
    
    desc_clean = truncate_text(item_row.get('description', ''), 55)
    
    # Update Sheet (No more B9/D9 count logic)
    updates = [
        {'range': 'C3', 'values': [[str(item_row.get('customer_sku', ''))]]},
        {'range': 'B5', 'values': [[desc_clean]]},
        {'range': 'B7', 'values': [[str(item_row.get('vendor_sku', ''))]]},
        {'range': 'C11', 'values': [[str(item_row.get('po_num', ''))]]},
        {'range': 'F7', 'values': [[label_qty]]}
    ]
    lbl_ws.batch_update(updates)
    time.sleep(0.8) # Wait for Google to save
    
    pdf_raw = export_sheet_to_pdf(LABEL_TEMPLATE_ID, lbl_ws.id, creds, fit=False)
    
    if pdf_raw:
        # Process Geometry (Crop/Rotate)
        reader = PdfReader(BytesIO(pdf_raw))
        page = reader.pages[0]
        
        op = Transformation().scale(sx=settings['scale'], sy=settings['scale']).translate(tx=settings['x'], ty=settings['y'])
        page.add_transformation(op)
        
        # Crop to 6x4 inches (432x288 pts)
        page.mediabox.lower_left = (0, page.mediabox.top - 288) 
        page.mediabox.upper_right = (432, page.mediabox.top)
        
        if settings['rotate']:
            page.rotate(90)
            
        return page
    return None

def warehouse_interface(client, creds):
    top_bar("Warehouse Control Center")
    
    # Load Data
    @st.cache_data(ttl=60)
    def load_data():
        try:
            sh = client.open_by_key(SOURCE_SHEET_ID)
            return pd.DataFrame(sh.worksheet("Open SO").get_all_records())
        except: return pd.DataFrame()

    if st.button("🔄 Refresh Orders"):
        st.cache_data.clear()
        st.rerun()

    df = load_data()
    if df.empty:
        st.info("No orders found.")
        return
        
    # --- VIEW 1: DASHBOARD (Interactive Table) ---
    if "selected_order" not in st.session_state:
        st.session_state.selected_order = None

    if st.session_state.selected_order is None:
        # Filter Box
        filter_txt = st.text_input("🔎 Quick Filter (Order # or Customer)", placeholder="Type to filter list...")
        
        # Prep Table
        view_df = df[['order_num', 'po_num', 'customer_name', 'vendor_sku', 'ordered_qty']].copy()
        # Group to remove duplicates
        grouped = view_df.groupby(['order_num', 'po_num', 'customer_name']).agg({
            'vendor_sku': 'count',
            'ordered_qty': 'sum'
        }).reset_index().rename(columns={'vendor_sku': 'Lines', 'ordered_qty': 'Total Qty'})
        
        if filter_txt:
            grouped = grouped[
                grouped['order_num'].astype(str).str.contains(filter_txt, case=False) | 
                grouped['customer_name'].str.contains(filter_txt, case=False)
            ]

        st.info("👆 Double-click a row to open the order.")
        
        # INTERACTIVE TABLE
        event = st.dataframe(
            grouped,
            use_container_width=True,
            hide_index=True,
            selection_mode="single-row",
            on_select="rerun"
        )
        
        # Handle Selection
        if len(event.selection.rows) > 0:
            selected_row_idx = event.selection.rows[0]
            order_id = grouped.iloc[selected_row_idx]['order_num']
            st.session_state.selected_order = str(order_id)
            st.rerun()

    # --- VIEW 2: ORDER DETAILS ---
    else:
        order_id = st.session_state.selected_order
        order_data = df[df['order_num'].astype(str) == str(order_id)].copy()
        
        if order_data.empty:
            st.error("Order not found.")
            st.session_state.selected_order = None
            st.rerun()
            
        header = order_data.iloc[0]

        # Order Header
        c1, c2 = st.columns([0.1, 0.9])
        with c1:
            if st.button("⬅️ Back"):
                st.session_state.selected_order = None
                st.rerun()
        with c2:
            st.markdown(f"## {header['customer_name']}") 
            st.caption(f"**Order:** {header['order_num']} | **PO:** {header['po_num']}")

        st.divider()

        # Layout: Table (Left) | Actions (Right)
        col_table, col_actions = st.columns([2, 1])

        # 1. CLEAN TABLE
        with col_table:
            st.subheader("Items")
            # Create a clean view for the user
            display_df = order_data[['vendor_sku', 'description', 'ordered_qty', 'customer_sku']].copy()
            # We keep customer_sku in data for logic, but we can hide it in display if needed. 
            # User asked to remove it from view.
            
            display_df['shipped_qty'] = display_df['ordered_qty'] # Default
            
            # Using data_editor but configuring visible columns
            edited_df = st.data_editor(
                display_df,
                column_config={
                    "vendor_sku": st.column_config.TextColumn("SKU", disabled=True),
                    "description": st.column_config.TextColumn("Description", disabled=True, width="medium"),
                    "ordered_qty": st.column_config.NumberColumn("Ord", disabled=True, width="small"),
                    "shipped_qty": st.column_config.NumberColumn("Ship", min_value=0, width="small"),
                    "customer_sku": None # Hides this column!
                },
                use_container_width=True,
                hide_index=True, # Hides the "0, 1, 2" column
                key=f"editor_{order_id}"
            )

        # 2. ACTION PANEL
        with col_actions:
            st.subheader("Actions")
            
            # --- PRINTER SETTINGS (Global) ---
            with st.expander("⚙️ Printer Settings (Zebra)"):
                p_rotate = st.checkbox("Rotate 90°", value=True)
                p_scale = st.slider("Scale", 0.5, 1.2, 0.95, 0.05)
                p_x = st.slider("Up/Down", -100, 100, -20, 5)
                p_y = st.slider("L/R", -100, 100, 0, 5)
                settings = {'rotate': p_rotate, 'scale': p_scale, 'x': p_x, 'y': p_y}

            tab_single, tab_batch, tab_slip = st.tabs(["🏷️ Single Label", "📦 Batch Labels", "📄 Packing Slip"])
            
            # A. SINGLE ITEM LABEL
            with tab_single:
                item_sku = st.selectbox("Select Item:", edited_df['vendor_sku'].unique())
                item_row = order_data[order_data['vendor_sku'] == item_sku].iloc[0]
                qty_box = st.number_input("Qty on Label", value=int(item_row.get('ordered_qty', 1)))
                
                if st.button(f"Print '{item_sku}'"):
                    with st.spinner("Generating PDF..."):
                        merger = PdfWriter()
                        page = generate_single_label_pdf(item_row, qty_box, creds, client, settings)
                        if page: merger.add_page(page)
                        
                        out = BytesIO()
                        merger.write(out)
                        st.download_button("⬇️ Download Label", out.getvalue(), f"Label_{item_sku}.pdf", mime="application/pdf")

            # B. BATCH LABELS (ALL)
            with tab_batch:
                st.info("Generates one PDF with labels for EVERY item in this order.")
                if st.button("🖨️ Print ALL Labels", type="primary"):
                    with st.spinner("Processing Batch... this may take a moment..."):
                        merger = PdfWriter()
                        # Iterate through the edited DF to get current items
                        for idx, row in edited_df.iterrows():
                            # Find original data for this SKU
                            sku = row['vendor_sku']
                            full_row = order_data[order_data['vendor_sku'] == sku].iloc[0]
                            qty = row['ordered_qty'] # Default to ordered qty for label
                            
                            page = generate_single_label_pdf(full_row, qty, creds, client, settings)
                            if page: merger.add_page(page)
                        
                        out = BytesIO()
                        merger.write(out)
                        st.success("Batch Ready!")
                        st.download_button("⬇️ Download Batch PDF", out.getvalue(), f"Batch_Labels_{order_id}.pdf", mime="application/pdf")

            # C. PACKING SLIP
            with tab_slip:
                method = st.radio("Ship Method", ["Small Parcel", "LTL"])
                if st.button("Generate Slip"):
                    with st.spinner("Creating Slip..."):
                        ps_sh = client.open_by_key(PACKING_SLIP_ID)
                        ps_ws = ps_sh.worksheet("Template")
                        
                        # Update Header
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
                        
                        # Update Lines
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
                            st.download_button("⬇️ Download Slip", pdf_bytes, f"PS_{order_id}.pdf", mime="application/pdf")

# --- MAIN ---
if "logged_in" not in st.session_state:
    st.session_state["logged_in"] = False

client, creds = get_gspread_client()

if not st.session_state["logged_in"]:
    st.title("🔐 Warehouse Login")
    with st.form("login"):
        u = st.text_input("Username")
        p = st.text_input("Password", type="password")
        if st.form_submit_button("Login"):
            if u in USERS and USERS[u][0] == p:
                st.session_state["logged_in"] = True
                st.session_state["username"] = u
                st.session_state["role"] = USERS[u][1]
                st.rerun()
            else: st.error("Invalid")
else:
    # Router
    if st.session_state["role"] == "manager":
        # Admin Interface (Kept simple for brevity, insert your admin code here if needed)
        # For now, just forwarding to warehouse or upload
        pg = st.sidebar.radio("Go to", ["Warehouse View", "Upload Data"])
        if pg == "Warehouse View": warehouse_interface(client, creds)
        else: st.write("Admin Upload Screen (Same as previous)")
    else:
        warehouse_interface(client, creds)
