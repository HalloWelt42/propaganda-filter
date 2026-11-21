"""Das Verfahren allein: eingesetzte Stellen auf 10 ms genau, gleiche Folgen ohne Stellen."""

from conftest import klang as ton

import numpy as np

from propagandafilter import klang


def _merkmale(pfad):
    return klang.merkmale(klang.dekodieren(pfad))


def test_eingesetzter_spot_auf_zehntel_sekunden(fassungen):
    a, b = _merkmale(fassungen["a"]), _merkmale(fassungen["b"])
    bild = klang.vergleichen(a, b)
    assert len(bild.nur_hier) == 1
    stelle = bild.nur_hier[0]
    assert abs(stelle.von - 20.0) <= 0.1 and abs(stelle.bis - 28.0) <= 0.1
    assert bild.anteil_gemeinsam > 0.8
    zurueck = klang.vergleichen(b, a)
    assert zurueck.nur_hier == []
    # nach dem Spot liegt der Inhalt in B 8 s früher
    assert any(abs(l.versatz + 8.0) <= 0.05 for l in bild.gemeinsam)


def test_andere_folge_hat_kaum_gemeinsames(fassungen):
    bild = klang.vergleichen(_merkmale(fassungen["b"]), _merkmale(fassungen["anders"]))
    assert bild.anteil_gemeinsam < 0.2


def test_zu_kurz():
    try:
        klang.merkmale(ton(3, 9).astype(np.float32))
    except klang.KlangFehler as f:
        assert "zu kurz" in str(f)
    else:
        raise AssertionError("zu kurzer Ton wurde angenommen")
