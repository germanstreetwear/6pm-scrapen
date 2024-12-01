import logging
import json
import os
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from bs4 import BeautifulSoup
import requests
import firebase_admin
from firebase_admin import credentials, firestore
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

# Logging-Konfiguration
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# Firebase initialisieren
try:
    firebase_credentials = os.getenv('FIREBASE_CREDENTIALS')
    if not firebase_credentials:
        raise ValueError("Die Umgebungsvariable FIREBASE_CREDENTIALS ist leer.")

    cred_dict = json.loads(firebase_credentials)
    cred = credentials.Certificate(cred_dict)
    firebase_admin.initialize_app(cred)
    db = firestore.client()
    logging.info("Erfolgreich mit Firestore verbunden.")
except Exception as e:
    logging.error(f"Fehler bei der Firebase-Initialisierung: {e}")
    db = None




# Selenium WebDriver mit Headless-Option konfigurieren
options = Options()
options.add_argument('--headless')
options.add_argument('--disable-gpu')
options.add_argument('window-size=1920x1080')
options.add_argument('--no-sandbox')
options.add_argument('--disable-blink-features=AutomationControlled')
options.add_argument('--disable-dev-shm-usage')
options.add_argument('--disable-infobars')
options.add_argument("--lang=de")

# Funktion, um alle Produkt-URLs von allen Seiten dynamisch zu sammeln
def get_all_product_urls(base_url, category_url_template, selectors):
    try:
        all_product_urls = []
        page_number = 1

        while True:
            category_url = category_url_template.format(page=page_number)
            logging.info(f"Scrape Kategorie-Seite: {category_url}")
            
            response = requests.get(category_url, timeout=10)
            if response.status_code != 200:
                logging.warning(f"Fehler beim Abrufen der Seite {category_url}: {response.status_code}")
                break

            soup = BeautifulSoup(response.text, 'html.parser')
            product_blocks = soup.select(selectors['product_block'])
            if not product_blocks:
                logging.info(f"Keine weiteren Produkte auf Seite {page_number}. Beende.")
                break
            
            for block in product_blocks:
                link_tag = block.select_one(selectors['product_link'])
                if link_tag and 'href' in link_tag.attrs:
                    product_url = base_url + link_tag['href']
                    all_product_urls.append(product_url)
            
            logging.info(f"Seite {page_number} gescraped, Produkte gefunden: {len(product_blocks)}")
            page_number += 1

        all_product_urls = list(set(all_product_urls))
        logging.info(f"Gesamtanzahl der gefundenen Produkt-URLs: {len(all_product_urls)}")
        return all_product_urls

    except Exception as e:
        logging.error(f"Fehler beim Abrufen der Produkt-URLs: {e}")
        return []


# Funktion, um Produktdetails zu extrahieren
def scrape_product_details(driver, product_url, selectors):
    try:
        driver.get(product_url)
        time.sleep(5)
        html = driver.page_source
        soup = BeautifulSoup(html, 'html.parser')
        
        product_name = soup.select_one(selectors['product_name']).text.strip() if soup.select_one(selectors['product_name']) else None
        description_elements = soup.select(selectors['product_description']) if selectors.get('product_description') else []
        stripe_description = " ".join([desc.text.strip() for desc in description_elements])
        
        image_divs = soup.select(selectors['image_gallery'])
        image_urls = []
        for img_div in image_divs:
            img_tags = img_div.find_all('img')
            for img in img_tags:
                img_src = img.get('src', '')
                if img_src:
                    if img_src.startswith('//'):
                        img_src = 'https:' + img_src
                    image_urls.append(img_src)
        
        size_elements = soup.select(selectors['size_options']) if selectors.get('size_options') else []
        sizes = {}
        for input_element in size_elements:
            size_name = input_element.get(selectors['size_value_attr'], '').strip()
            label_id = input_element.get('id')
            label_element = soup.find('label', {'for': label_id})
            if label_element and 'is-disabled' in label_element.get('class', []):
                sizes[size_name] = False
            else:
                sizes[size_name] = True
        
        # Debugging für Preisextraktion
        price_tag = soup.select_one(selectors['price'])
        if price_tag:
            price_text = price_tag.text.strip()
            logging.debug(f"Rohpreistext extrahiert: {price_text}")
            try:
                # Entferne unerwünschte Zeichen und konvertiere den Preis
                price_cleaned = price_text.replace("Sale price", "").replace("€", "").replace(",", ".").strip()
                logging.debug(f"Bereinigter Preistext: {price_cleaned}")
                price = int(float(price_cleaned) * 100)  # Umrechnung in Cents
                logging.debug(f"Konvertierter Preis in Cents: {price}")
            except (ValueError, TypeError) as e:
                logging.error(f"Fehler beim Konvertieren des Preises: {price_text} - {e}")
                price = "sold_out"
        else:
            logging.warning(f"Preis konnte nicht gefunden werden. URL: {product_url}")
            logging.debug(f"HTML-Inhalt des Produkts:\n{html[:1000]}")  # Zeigt die ersten 1000 Zeichen an
            price = "sold_out"
        
        logging.info(f"Produktdetails gescraped: {product_name}")
        
        # Zeitstempel hinzufügen
        timestamp = datetime.now(timezone.utc).isoformat()

        # Rückgabe der Produktdetails
        return {
            "name": product_name,
            "description": stripe_description,
            "images": image_urls,
            "sizes": sizes,
            "price": price,
            "url": product_url,
            "timestamp": timestamp
        }
    except Exception as e:
        logging.error(f"Fehler beim Scrapen der Produktdetails ({product_url}): {e}")
        return {}




# Funktion, um bestehende Produkte aus Firebase abzurufen
def get_existing_products(company_name):
    try:
        doc_ref = db.collection("companies").document(company_name)
        current_data = doc_ref.get()
        if current_data.exists:
            return current_data.to_dict().get("products", {})
        else:
            return {}
    except Exception as e:
        logging.error(f"Fehler beim Abrufen bestehender Produkte aus Firestore: {e}")
        return {}

# Funktion, um nicht mehr existierende Produkte aus Firebase zu löschen
def delete_removed_products(company_name, current_product_names):
    try:
        doc_ref = db.collection("companies").document(company_name)
        current_data = doc_ref.get()
        if current_data.exists:
            existing_products = current_data.to_dict().get("products", {})
            products_to_delete = [name for name in existing_products.keys() if name not in current_product_names]
            for product_name in products_to_delete:
                logging.info(f"Entferne Produkt aus Firebase: {product_name}")
                del existing_products[product_name]
            doc_ref.set({"products": existing_products}, merge=True)
            logging.info(f"Veraltete Produkte für {company_name} wurden entfernt.")
        else:
            logging.info(f"Keine vorhandenen Produkte für {company_name} gefunden.")
    except Exception as e:
        logging.error(f"Fehler beim Entfernen von Produkten aus Firestore: {e}")


# Funktion zum Scrapen und Speichern der Produkte
def scrape_and_store_all_products(shop_data):
    base_url = shop_data['base_url']
    category_url = shop_data['category_url']
    selectors = shop_data['selectors']
    company_name = shop_data.get('company_name', 'Unknown Company')
    
    existing_products = get_existing_products(company_name)
    current_product_names = set()
    
    product_urls = get_all_product_urls(base_url, category_url, selectors)
    driver = webdriver.Chrome(options=options)
    try:
        for url in product_urls:
            product_details = scrape_product_details(driver, url, selectors)
            if product_details.get("name"):
                current_product_names.add(product_details["name"])
                save_to_firestore(company_name, {product_details["name"]: product_details})
    finally:
        driver.quit()
    
    delete_removed_products(company_name, current_product_names)


# Funktion zum Speichern in Firestore
def save_to_firestore(company_name, product_data):
    if not db:
        logging.error("Firestore ist nicht initialisiert.")
        return
    try:
        doc_ref = db.collection("companies").document(company_name)
        current_data = doc_ref.get()
        if current_data.exists:
            existing_products = current_data.to_dict().get("products", {})
        else:
            existing_products = {}
        
        for product_name, product_details in product_data.items():
            if product_name not in existing_products:
                # Markiere das Produkt als neu
                product_details["new"] = True
            else:
                # Entferne das "new"-Flag, falls es existiert
                if "new" in existing_products[product_name]:
                    product_details["new"] = existing_products[product_name]["new"]
        
        # Aktualisiere die Produktdaten in Firestore
        existing_products.update(product_data)
        doc_ref.set({"products": existing_products}, merge=True)
        logging.info(f"Produktdaten für {company_name} gespeichert.")
    except Exception as e:
        logging.error(f"Fehler beim Speichern in Firestore: {e}")



# Parallelisiertes Scraping für mehrere Shops
def scrape_multiple_shops(shops):
    with ThreadPoolExecutor(max_workers=3) as executor:
        executor.map(scrape_and_store_all_products, shops)


# Shops definieren
shops = [
    {
        "base_url": "https://www.6pmseason.com/",
        "category_url": "https://www.6pmseason.com/collections/6pm?page={page}",
        "company_name": "6PM",
        "selectors": {
            "product_block": ".product-card",
            "product_link": ".product-card__media",
            "product_name": ".product-info__block-item .product-title",
            "product_description": ".accordion__content.prose",
            "image_gallery": ".product-gallery__image-list .product-gallery__carousel",
            "size_options": ".variant-picker__option-values input[type='radio']",
            "size_value_attr": "value",
          "price": "sale-price.h5"

        }
    }
]

# Starten
scrape_multiple_shops(shops)
