import base64
import hashlib
import hmac
import io
import json
import secrets
import smtplib
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path

import streamlit as st
from PIL import Image, ImageChops
from pypdf import PdfReader, PdfWriter
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas
from streamlit_drawable_canvas import st_canvas


APP_TITLE = "Firma mobile modulo FIPAV - Prototipo"
DATA_DIR = Path("dati_firma_prototipo")
INDEX_FILE = DATA_DIR / "richieste.json"
DEFAULT_TEST_RECIPIENT = "cucigno63@yahoo.it"

# Coordinate in punti PDF, misurate sul Modulo F A4 allegato.
# Ogni pagina contiene tre atlete; il prototipo firma il riquadro del genitore.
PARENT_BOXES = {
    1: (400, 450, 160, 16),
    2: (400, 288, 160, 16),
    3: (400, 126, 160, 16),
}


def load_index():
    try:
        data = json.loads(INDEX_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_index(data):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = INDEX_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(INDEX_FILE)


def safe_name(value):
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in str(value)).strip("_") or "documento"


def crop_signature(image):
    image = image.convert("RGBA")
    white = Image.new("RGBA", image.size, (255, 255, 255, 255))
    diff = ImageChops.difference(image, white).convert("L")
    bbox = diff.point(lambda p: 255 if p > 20 else 0).getbbox()
    if not bbox:
        raise ValueError("Firma vuota: firma nel riquadro prima di confermare.")
    cropped = image.crop(bbox)
    px = cropped.load()
    for y in range(cropped.height):
        for x in range(cropped.width):
            r, g, b, _ = px[x, y]
            alpha = 255 - min(r, g, b)
            px[x, y] = (15, 35, 90, alpha)
    return cropped


def sign_pdf(pdf_bytes, page_number, slot, signature_image, signer, signed_at):
    reader = PdfReader(io.BytesIO(pdf_bytes), strict=False)
    if page_number < 1 or page_number > len(reader.pages):
        raise ValueError("Pagina non valida.")
    x, y, w, h = PARENT_BOXES[int(slot)]
    sig = crop_signature(signature_image)
    sig_buffer = io.BytesIO()
    sig.save(sig_buffer, format="PNG")
    sig_buffer.seek(0)

    page = reader.pages[page_number - 1]
    page_w = float(page.mediabox.width)
    page_h = float(page.mediabox.height)
    overlay_buffer = io.BytesIO()
    overlay = canvas.Canvas(overlay_buffer, pagesize=(page_w, page_h))
    margin = 3
    scale = min((w - 2 * margin) / sig.width, (h - 5) / sig.height)
    draw_w, draw_h = sig.width * scale, sig.height * scale
    draw_x = x + (w - draw_w) / 2
    draw_y = y + 3 + (h - 3 - draw_h) / 2
    overlay.drawImage(ImageReader(sig_buffer), draw_x, draw_y, width=draw_w, height=draw_h, mask="auto")
    overlay.save()
    overlay_buffer.seek(0)
    page.merge_page(PdfReader(overlay_buffer).pages[0])

    # Il PDF FIPAV contiene un AcroForm incompleto: la clonazione dell'intero
    # documento puo perdere il contenuto grafico. Copiamo invece le pagine gia
    # risolte, mantenendo intatto il modulo e la sovrapposizione.
    writer = PdfWriter()
    for source_page in reader.pages:
        writer.add_page(source_page)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def send_signed_pdf(recipient, athlete, pdf_bytes, filename):
    """Invia il PDF tramite Gmail usando esclusivamente i Secrets di Streamlit."""
    try:
        email_cfg = st.secrets.get("email", {})
        sender = str(email_cfg.get("sender", "promozionale.mv@gmail.com") or "").strip()
        app_password = str(email_cfg.get("app_password", "") or "").replace(" ", "")
    except Exception:
        sender, app_password = "", ""
    recipient = str(recipient or "").strip().lower()
    if not sender or not app_password:
        raise RuntimeError("configura [email] sender e app_password nei Secrets di Streamlit")
    if "@" not in recipient:
        raise RuntimeError("indirizzo e-mail destinatario non valido")

    message = EmailMessage()
    message["From"] = sender
    message["To"] = recipient
    message["Subject"] = f"Modulo F FIPAV firmato - {athlete}"
    message.set_content(
        f"Buongiorno,\n\n"
        f"in allegato trova il Modulo F FIPAV firmato relativo all'atleta {athlete}.\n\n"
        f"Cordiali saluti\nMonviso Volley"
    )
    message.add_attachment(pdf_bytes, maintype="application", subtype="pdf", filename=filename)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as smtp:
        smtp.login(sender, app_password)
        smtp.send_message(message)


def create_request(pdf_bytes, athlete, parent_email, page_number, slot, base_url):
    token = secrets.token_urlsafe(24)
    request_dir = DATA_DIR / token
    request_dir.mkdir(parents=True, exist_ok=False)
    original = request_dir / "originale.pdf"
    original.write_bytes(pdf_bytes)
    digest = hashlib.sha256(pdf_bytes).hexdigest()
    record = {
        "token": token,
        "atleta": athlete.strip().upper(),
        "email_genitore": parent_email.strip().lower(),
        "pagina": int(page_number),
        "posizione": int(slot),
        "creata_il": datetime.now().strftime("%d/%m/%Y %H:%M:%S"),
        "stato": "DA FIRMARE",
        "pdf_originale": str(original),
        "hash_originale": digest,
        "invio": "NON INVIATO - PROTOTIPO",
    }
    index = load_index()
    index[token] = record
    save_index(index)
    return record, f"{base_url.rstrip('/')}?token={token}"


def signature_page(token):
    index = load_index()
    record = index.get(token)
    if not record:
        st.error("Collegamento non valido o scaduto.")
        return

    st.title("✍️ Firma modulo FIPAV")
    st.info(f"Atleta: **{record['atleta']}**")
    st.caption("Sono mostrati soltanto i dati della propria figlia. Il modulo completo non viene condiviso.")
    if record.get("stato") == "FIRMATO":
        st.success(f"Firma acquisita il {record.get('firmato_il', '')}.")
        signed_path = Path(record.get("pdf_firmato", ""))
        if signed_path.exists():
            st.download_button("📥 Scarica PDF firmato (prova)", signed_path.read_bytes(), file_name=signed_path.name, mime="application/pdf", use_container_width=True)
        return

    signer = st.text_input("Nome e cognome del genitore che firma")
    consent = st.checkbox("Confermo di essere il genitore/tutore dell’atleta e di voler sottoscrivere il modulo.")
    st.markdown("**Firma nel riquadro usando il dito o il pennino:**")
    drawing = st_canvas(
        stroke_width=3,
        stroke_color="#0f235a",
        background_color="#ffffff",
        height=170,
        width=340,
        drawing_mode="freedraw",
        key="firma_canvas_mobile",
    )
    st.caption("Se devi rifare la firma, premi il cestino nel riquadro.")
    if st.button("✅ CONFERMA FIRMA E GENERA PDF", type="primary", use_container_width=True, disabled=not consent):
        if len(signer.strip()) < 5:
            st.error("Inserisci nome e cognome del firmatario.")
            return
        if drawing.image_data is None:
            st.error("Firma nel riquadro prima di confermare.")
            return
        try:
            original = Path(record["pdf_originale"]).read_bytes()
            signed_at = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
            signed = sign_pdf(original, record["pagina"], record["posizione"], Image.fromarray(drawing.image_data.astype("uint8")), signer.strip().upper(), signed_at)
            signed_path = Path(record["pdf_originale"]).with_name(f"firmato_{safe_name(record['atleta'])}.pdf")
            signed_path.write_bytes(signed)
            record.update({
                "stato": "FIRMATO",
                "firmatario": signer.strip().upper(),
                "firmato_il": signed_at,
                "pdf_firmato": str(signed_path),
                "hash_firmato": hashlib.sha256(signed).hexdigest(),
                "invio": "DA INVIARE",
            })
            index[token] = record
            save_index(index)
            try:
                send_signed_pdf(
                    record.get("email_genitore"), record["atleta"], signed, signed_path.name
                )
                record["invio"] = f"INVIATO a {record['email_genitore']} il {signed_at}"
                st.success(f"Firma acquisita. PDF inviato a {record['email_genitore']}.")
            except Exception as mail_exc:
                record["invio"] = f"ERRORE INVIO: {mail_exc}"
                st.warning(f"PDF generato, ma e-mail non inviata: {mail_exc}")
            index[token] = record
            save_index(index)
            st.download_button("📥 SCARICA PDF FIRMATO", signed, file_name=signed_path.name, mime="application/pdf", use_container_width=True)
        except Exception as exc:
            st.error(f"Impossibile generare il PDF: {exc}")


def admin_page():
    st.title("🧪 Prototipo firma mobile FIPAV")
    st.info("Ambiente di prova: dopo la firma il PDF viene inviato all'indirizzo indicato.")
    uploaded = st.file_uploader("Carica il Modulo F FIPAV", type=["pdf"])
    athlete = st.text_input("Atleta della prova", value="CAMMARATA SOFIA")
    parent_email = st.text_input("E-mail destinataria della prova", value=DEFAULT_TEST_RECIPIENT)
    c1, c2 = st.columns(2)
    page_number = c1.number_input("Pagina del PDF", min_value=1, value=1, step=1)
    slot = c2.selectbox("Posizione atleta nella pagina", [1, 2, 3], format_func=lambda x: f"{x}ª atleta")
    base_url = st.text_input("Indirizzo pubblico del prototipo", placeholder="https://nome-app.streamlit.app")
    if st.button("🔗 CREA COLLEGAMENTO DI FIRMA", type="primary", use_container_width=True):
        if not uploaded:
            st.error("Carica prima il PDF.")
        elif not athlete.strip():
            st.error("Indica l’atleta.")
        elif not base_url.startswith("http"):
            st.error("Inserisci l’indirizzo pubblico completo del prototipo.")
        else:
            record, link = create_request(uploaded.getvalue(), athlete, parent_email, page_number, slot, base_url)
            st.success("Collegamento creato. Aprilo dal telefono per firmare.")
            st.code(link, language=None)
            st.link_button("📱 APRI PAGINA DI FIRMA", link, use_container_width=True)

    st.divider()
    st.subheader("Stato richieste di prova")
    index = load_index()
    if index:
        rows = [{
            "Atleta": r.get("atleta"), "Stato": r.get("stato"), "Creata": r.get("creata_il"),
            "Firmata": r.get("firmato_il", ""), "Firmatario": r.get("firmatario", ""), "Invio": r.get("invio", "")
        } for r in reversed(list(index.values()))]
        st.dataframe(rows, use_container_width=True, hide_index=True)
    else:
        st.info("Nessuna richiesta creata.")


def admin_authorized():
    """Protegge la pagina principale; i link tokenizzati restano apribili dai genitori."""
    try:
        expected = str(st.secrets.get("PROTOTYPE_ADMIN_PASSWORD", "") or "")
    except Exception:
        expected = ""
    if not expected:
        st.error("Password amministratore non configurata nei Secrets.")
        st.code('PROTOTYPE_ADMIN_PASSWORD = "inserisci-qui-una-password-lunga"', language="toml")
        return False
    if st.session_state.get("prototype_admin_ok"):
        return True
    st.title("🔐 Accesso amministratore")
    password = st.text_input("Password", type="password")
    if st.button("ACCEDI", type="primary", use_container_width=True):
        if hmac.compare_digest(password, expected):
            st.session_state["prototype_admin_ok"] = True
            st.rerun()
        else:
            st.error("Password non corretta.")
    return False


st.set_page_config(page_title=APP_TITLE, page_icon="✍️", layout="centered")
token = st.query_params.get("token", "")
if isinstance(token, list):
    token = token[0] if token else ""
if token:
    signature_page(str(token))
elif admin_authorized():
    admin_page()
