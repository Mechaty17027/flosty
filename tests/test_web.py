"""Address normalization needs no browser or network access."""
import unittest

from mechahub.core.web import web_target


class WebTargetTests(unittest.TestCase):
    def test_bare_domain_and_address_preserve_path_query_and_fragment(self):
        self.assertEqual(web_target(' example.com/page?q=hello#part '), 'https://example.com/page?q=hello#part')
        self.assertEqual(web_target('HTTP://EXAMPLE.COM:8080/a'), 'http://example.com:8080/a')
        self.assertEqual(web_target('https://[::1]:443/a'), 'https://[::1]:443/a')

    def test_search_phrase_is_encoded_as_query(self):
        self.assertEqual(web_target('Qt & Python'), 'https://duckduckgo.com/?q=Qt+%26+Python')
        self.assertIn('%D', web_target('погода Київ'))

    def test_invalid_address_or_nonweb_scheme_is_rejected(self):
        for value in ('', 'https://', 'ftp://example.com', 'file:///C:/test.txt',
                      'javascript:alert(1)', 'https://a:password@example.com',
                      'https://example.com:99999', 'https://bad host.com', 'https://example.com\n/a'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                web_target(value)


if __name__ == '__main__':
    unittest.main()
