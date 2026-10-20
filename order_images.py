"""Shared product images for downloaded and supplier order forms."""
import hashlib
from pathlib import Path
from io import BytesIO
from sqlalchemy import inspect,select
def product_images(app,db,order):
    table=db.metadata.tables.get("product_quote_image")
    ready=table is not None and inspect(db.session.connection()).has_table(table.name)
    result={}
    for item in order.items:
        blob=None
        if item.product:
            if ready: blob=db.session.execute(select(table.c.content).where(table.c.product_id==item.product_id)).scalar()
            if not blob:
                from product_image_assets import default_image_path
                path=default_image_path(app.root_path,item.product)
                if path:blob=path.read_bytes()
        if blob:
            digest=hashlib.sha256(blob).hexdigest()[:24]
            key=f"bos:order:{order.id}:item:{item.id}"
            result[key]=dict(content=blob,name=f"Urun-{item.product_id}-{digest}.jpg",key=key+":img:"+digest,product_id=item.product_id)
    return result

def pdf_image(blob,width=72,height=64):
    from reportlab.platypus import Image
    from PIL import Image as PILImage
    try:
        with PILImage.open(BytesIO(blob)) as im:
            im.load();im.thumbnail((600,600));output=BytesIO();im.convert('RGB').save(output,format='JPEG',quality=85)
        image=Image(BytesIO(output.getvalue()))
        ratio=min(width/image.imageWidth,height/image.imageHeight)
        image.drawWidth=image.imageWidth*ratio;image.drawHeight=image.imageHeight*ratio
        image.hAlign='LEFT'
        return image
    except (OSError,ValueError):return None
